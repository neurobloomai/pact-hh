"""
pact_hh/loop.py
─────────────────
HumanEscalationLoop — the main entry point for pact-hh.

This is the one class you wire into your PACT stack. It:

  1. Subscribes to the CoordinationBus for CONSENSUS_FAILED /
     ESCALATION_TRIGGERED / POLICY_VIOLATED events
  2. Builds an EscalationPacket from the event payload
  3. Routes it to the right human + channel via EscalationRouter
  4. Delivers it via the appropriate HumanChannel
  5. Tracks the open escalation in EscalationStore (SLA enforcement)
  6. On reply: parses via HumanResponseAdapter
  7. Closes the loop via DecisionInjector → CoordinationBus + TrustNetwork

Usage — minimal
───────────────
    from pact_hh.loop import HumanEscalationLoop

    loop = HumanEscalationLoop.create(
        slack_token      = "xoxb-...",
        default_human_id = "on-call-manager",
    )
    loop.start()          # subscribes to bus, starts SLA ticker (background)

    # When a human replies:
    loop.handle_reply(slack_payload, channel="slack")

Usage — full stack
──────────────────
    from pact_ax.coordination import CoordinationBus, TrustNetwork
    from pact_hh.loop import HumanEscalationLoop, LoopConfig
    from pact_hh.escalation_router import RoutingRule

    bus   = CoordinationBus()
    trust = TrustNetwork()

    loop = HumanEscalationLoop(
        config = LoopConfig(sla_minutes=30, reminder_at_minutes=[15, 5]),
        bus    = bus,
        trust  = trust,
        rules  = [
            RoutingRule(intent_pattern="approve_refund", human_id="finance-lead@company.com", channel="email"),
            RoutingRule(trigger="policy_violated",       human_id="legal-team",               channel="slack"),
        ],
    )
    loop.start()
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pact_hh.decision_injector import DecisionInjector, InjectionResult
from pact_hh.escalation_packet import (
    AgentVote,
    EscalationPacket,
    EscalationTrigger,
)
from pact_hh.escalation_router import EscalationRouter, RoutingAssignment, RoutingRule
from pact_hh.escalation_store import EscalationStore
from pact_hh.human_channels.base import ChannelRegistry, DeliveryReceipt
from pact_hh.human_channels.slack import SlackChannel
from pact_hh.human_channels.email import EmailChannel
from pact_hh.human_channels.webhook import WebhookChannel
from pact_hh.response_adapter import HumanResponseAdapter

logger = logging.getLogger(__name__)

# Try to import pact-ax CoordinationBus event types
try:
    from pact_ax.coordination.coordination_bus import CoordinationBus, EventType
    _BUS_AVAILABLE = True
except ImportError:
    CoordinationBus = None
    EventType = None
    _BUS_AVAILABLE = False


# ── Configuration ──────────────────────────────────────────────────────────────

@dataclass
class LoopConfig:
    """
    Configuration knobs for HumanEscalationLoop.

    Parameters
    ----------
    sla_minutes         : Default SLA for new escalations (minutes). Default 30.
    reminder_at_minutes : Fire a reminder when this many minutes remain.
    tick_interval_secs  : How often the SLA ticker runs. Default 60 seconds.
    default_human_id    : Fallback assignee when no routing rule matches.
    default_channel     : Fallback channel when no routing rule matches.
    """
    sla_minutes:          int        = 30
    reminder_at_minutes:  List[int]  = field(default_factory=lambda: [15, 5])
    tick_interval_secs:   int        = 60
    default_human_id:     str        = "on-call"
    default_channel:      str        = "slack"


# ── Delivery outcome ───────────────────────────────────────────────────────────

@dataclass
class EscalationOutcome:
    """Result of a single escalation dispatch."""
    escalation_id: str
    routed_to:     str
    channel:       str
    delivered:     bool
    receipt:       Optional[DeliveryReceipt] = None
    error:         Optional[str]             = None


# ── Main loop class ────────────────────────────────────────────────────────────

class HumanEscalationLoop:
    """
    The orchestrating class for the pact-hh human escalation protocol.

    Parameters
    ----------
    config    : LoopConfig — all tunable settings.
    rules     : List of RoutingRule — who handles which escalation type.
    bus       : CoordinationBus from pact-ax (optional).
    trust     : TrustNetwork from pact-ax (optional).
    channels  : Pre-built ChannelRegistry (optional — built from config if None).
    """

    def __init__(
        self,
        config:   LoopConfig             = None,
        rules:    List[RoutingRule]       = None,
        bus:      Optional[Any]           = None,
        trust:    Optional[Any]           = None,
        channels: Optional[ChannelRegistry] = None,
    ) -> None:
        self._config  = config or LoopConfig()
        self._bus     = bus
        self._trust   = trust

        # Sub-components
        self._router  = EscalationRouter(
            rules             = rules or [],
            default_human_id  = self._config.default_human_id,
            default_channel   = self._config.default_channel,
        )
        self._store   = EscalationStore(
            reminder_at_minutes = self._config.reminder_at_minutes,
            on_timeout          = self._on_sla_timeout,
            on_reminder         = self._on_sla_reminder,
        )
        self._channels  = channels or ChannelRegistry()
        self._adapter   = HumanResponseAdapter(store=self._store)
        self._injector  = DecisionInjector(
            store = self._store,
            bus   = self._bus,
            trust = self._trust,
        )

        self._ticker_thread: Optional[threading.Thread] = None
        self._running = False

        # Stats
        self._stats: Dict[str, int] = {
            "escalated":  0,
            "resolved":   0,
            "timed_out":  0,
            "reminders":  0,
        }

    # ── Factory constructors ───────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        slack_token:       str = "",
        slack_channel:     str = "#escalations",
        default_human_id:  str = "on-call",
        rules:             List[RoutingRule] = None,
        bus:               Optional[Any] = None,
        trust:             Optional[Any] = None,
        dry_run:           bool = False,
    ) -> "HumanEscalationLoop":
        """
        Convenience constructor — builds a fully wired loop with Slack enabled.
        """
        channels = ChannelRegistry()
        channels.register(SlackChannel(
            bot_token       = slack_token,
            default_channel = slack_channel,
            dry_run         = dry_run or not slack_token,
        ))
        channels.register(EmailChannel(dry_run=dry_run))
        channels.register(WebhookChannel(dry_run=dry_run))

        return cls(
            config   = LoopConfig(default_human_id=default_human_id),
            rules    = rules or [],
            bus      = bus,
            trust    = trust,
            channels = channels,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to the bus and start the SLA ticker thread."""
        if self._running:
            return

        self._running = True
        self._subscribe_to_bus()
        self._start_ticker()
        logger.info(
            "HumanEscalationLoop started "
            "(rules=%d, sla=%d min, tick=%ds, channels=%s)",
            len(self._router._rules),
            self._config.sla_minutes,
            self._config.tick_interval_secs,
            self._channels.available(),
        )

    def stop(self) -> None:
        """Unsubscribe and stop the ticker thread."""
        self._running = False
        if self._ticker_thread and self._ticker_thread.is_alive():
            self._ticker_thread.join(timeout=5)
        logger.info("HumanEscalationLoop stopped")

    # ── Inbound — new escalation event ────────────────────────────────────────

    def escalate(
        self,
        trigger:     EscalationTrigger,
        intent:      str,
        session_id:  str,
        agent_votes: List[Dict]          = None,
        context:     Dict[str, Any]      = None,
        recommended: Optional[str]       = None,
        sla_minutes: Optional[int]       = None,
        metadata:    Dict[str, Any]      = None,
    ) -> EscalationOutcome:
        """
        Open a new escalation and deliver it to the right human.

        This is the primary programmatic entry point — call this when you
        want to trigger a human escalation without going through the bus.
        """
        votes = [
            AgentVote(
                agent_id   = v.get("agent_id", "unknown"),
                decision   = v.get("decision", ""),
                confidence = float(v.get("confidence", 0.5)),
                reasoning  = v.get("reasoning", ""),
            )
            for v in (agent_votes or [])
        ]

        packet = EscalationPacket(
            trigger    = trigger,
            intent     = intent,
            session_id = session_id,
            agent_votes = votes,
            context     = context or {},
            recommended = recommended,
            sla_minutes = sla_minutes or self._config.sla_minutes,
            metadata    = metadata or {},
        )

        return self._dispatch(packet)

    def escalate_from_packet(self, packet: EscalationPacket) -> EscalationOutcome:
        """Dispatch a pre-built EscalationPacket."""
        return self._dispatch(packet)

    # ── Inbound — human reply ─────────────────────────────────────────────────

    def handle_reply(
        self,
        raw:     Dict[str, Any],
        channel: str = "slack",
    ) -> Optional[InjectionResult]:
        """
        Process a human's reply from any channel.

        raw     : The raw event payload (Slack payload, email dict, webhook body).
        channel : Which channel this came from ("slack", "email", "webhook").

        Returns an InjectionResult if parsed successfully, None otherwise.
        """
        if channel == "slack":
            decision = self._adapter.from_slack(raw)
        elif channel == "email":
            decision = self._adapter.from_email(raw)
        elif channel == "webhook":
            decision = self._adapter.from_webhook(raw)
        else:
            decision = self._adapter.from_dict(raw)

        if decision is None:
            logger.debug("handle_reply: could not parse response from channel %r", channel)
            return None

        result = self._injector.inject(decision)
        if result.success:
            self._stats["resolved"] += 1

        return result

    def handle_text_reply(
        self,
        text:          str,
        escalation_id: str,
        human_id:      str,
        channel:       str = "unknown",
    ) -> Optional[InjectionResult]:
        """
        Process a plain-text reply (useful for CLI, custom UI, testing).
        """
        decision = self._adapter.from_text(
            text          = text,
            escalation_id = escalation_id,
            human_id      = human_id,
            channel       = channel,
        )
        if decision is None:
            return None

        result = self._injector.inject(decision)
        if result.success:
            self._stats["resolved"] += 1
        return result

    # ── Dispatch pipeline ──────────────────────────────────────────────────────

    def _dispatch(self, packet: EscalationPacket) -> EscalationOutcome:
        """Route → open in store → deliver via channel."""
        # 1. Route
        assignment = self._router.route(packet)

        # 2. Open in store (SLA starts now)
        self._store.open(
            packet      = packet,
            assigned_to = assignment.human_id,
            channel     = assignment.channel,
        )
        self._stats["escalated"] += 1

        # 3. Render body (plain text fallback — pact-hx would render here)
        body = packet.plain_text()

        # 4. Deliver via channel
        receipt = self._deliver(packet, assignment, body)

        outcome = EscalationOutcome(
            escalation_id = packet.escalation_id,
            routed_to     = assignment.human_id,
            channel       = assignment.channel,
            delivered     = receipt.delivered if receipt else False,
            receipt       = receipt,
            error         = receipt.error if receipt else "no channel registered",
        )

        logger.info(
            "Escalation %s dispatched → %s via %s (delivered=%s)",
            packet.escalation_id, assignment.human_id,
            assignment.channel, outcome.delivered,
        )
        return outcome

    def _deliver(
        self,
        packet:     EscalationPacket,
        assignment: RoutingAssignment,
        body:       str,
    ) -> Optional[DeliveryReceipt]:
        """Find the right channel and deliver."""
        try:
            channel = self._channels.get(assignment.channel)
            return channel._safe_send(packet, assignment, body)
        except KeyError:
            logger.warning(
                "No channel registered for %r — falling back to default %r",
                assignment.channel, self._config.default_channel,
            )
            try:
                channel = self._channels.get(self._config.default_channel)
                return channel._safe_send(packet, assignment, body)
            except KeyError:
                logger.error(
                    "No channels available to deliver escalation %s",
                    packet.escalation_id,
                )
                return None

    # ── SLA ticker ─────────────────────────────────────────────────────────────

    def _start_ticker(self) -> None:
        def _tick_loop():
            while self._running:
                try:
                    counts = self._store.tick()
                    if counts["timeouts"] or counts["reminders"]:
                        self._stats["timed_out"] += counts["timeouts"]
                        self._stats["reminders"] += counts["reminders"]
                except Exception as exc:
                    logger.error("SLA ticker error: %s", exc)
                time.sleep(self._config.tick_interval_secs)

        self._ticker_thread = threading.Thread(
            target   = _tick_loop,
            name     = "pact-hh-sla-ticker",
            daemon   = True,
        )
        self._ticker_thread.start()

    def _on_sla_timeout(self, record) -> None:
        """Called when an escalation's SLA expires without a human response."""
        logger.warning(
            "SLA EXPIRED: escalation %s (intent=%r, assigned=%s) "
            "timed out after %.0f min",
            record.escalation_id, record.packet.intent,
            record.assigned_to, record.age_minutes,
        )
        # Optionally publish to bus
        if self._bus and _BUS_AVAILABLE:
            try:
                self._bus.publish(
                    EventType.ESCALATION_TRIGGERED,
                    source  = "pact-hh",
                    payload = {
                        "event":          "sla_timeout",
                        "escalation_id":  record.escalation_id,
                        "intent":         record.packet.intent,
                        "assigned_to":    record.assigned_to,
                        "age_minutes":    record.age_minutes,
                    },
                )
            except Exception as exc:
                logger.error("Failed to publish SLA timeout event: %s", exc)

    def _on_sla_reminder(self, record) -> None:
        """Called at reminder thresholds — re-deliver a nudge to the human."""
        logger.info(
            "SLA REMINDER #%d: escalation %s (%.0f min remaining, assigned=%s)",
            record.reminder_count + 1, record.escalation_id,
            record.minutes_remaining, record.assigned_to,
        )
        # Re-deliver a simplified reminder via the same channel
        try:
            channel = self._channels.get(record.channel or self._config.default_channel)
            reminder_body = (
                f"⏰ Reminder: Decision still needed for {record.packet.intent}\n"
                f"ID: {record.escalation_id} | "
                f"{record.minutes_remaining:.0f} minutes remaining"
            )
            assignment = RoutingAssignment(
                human_id     = record.assigned_to or self._config.default_human_id,
                channel      = record.channel or self._config.default_channel,
                rule_matched = "sla_reminder",
            )
            channel._safe_send(record.packet, assignment, reminder_body)
        except Exception as exc:
            logger.error("Reminder delivery failed: %s", exc)

    # ── Bus subscription ───────────────────────────────────────────────────────

    def _subscribe_to_bus(self) -> None:
        if not self._bus or not _BUS_AVAILABLE:
            return

        triggers_map = {
            EventType.CONSENSUS_FAILED:      EscalationTrigger.CONSENSUS_FAILED,
            EventType.ESCALATION_TRIGGERED:  EscalationTrigger.LOW_CONFIDENCE,
            EventType.POLICY_VIOLATED:       EscalationTrigger.POLICY_VIOLATED,
        }

        for event_type, trigger in triggers_map.items():
            self._bus.subscribe(
                event_type,
                lambda event, t=trigger: self._on_bus_event(event, t),
            )
            logger.debug("Subscribed to bus event: %s", event_type)

    def _on_bus_event(self, event: Any, trigger: EscalationTrigger) -> None:
        """Handle a CoordinationBus event by opening a new escalation."""
        try:
            payload = getattr(event, "payload", {}) or {}
            self.escalate(
                trigger     = trigger,
                intent      = payload.get("intent", payload.get("last_intent", "unknown")),
                session_id  = payload.get("session_id", "unknown"),
                agent_votes = payload.get("agent_votes", []),
                context     = payload.get("context", {}),
                recommended = payload.get("recommended"),
                metadata    = {"source_event": str(getattr(event, "event_type", ""))},
            )
        except Exception as exc:
            logger.error("Failed to handle bus event %s: %s", event, exc)

    # ── Observability ──────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            **self._stats,
            "store":   self._store.metrics(),
            "router":  self._router.stats(),
            "channels": self._channels.healthy(),
        }

    def health(self) -> Dict[str, Any]:
        return {
            "running":          self._running,
            "open_escalations": len(self._store),
            "channels":         self._channels.healthy(),
        }

    def __repr__(self) -> str:
        return (
            f"HumanEscalationLoop("
            f"running={self._running}, "
            f"open={len(self._store)}, "
            f"channels={self._channels.available()})"
        )
