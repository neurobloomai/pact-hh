"""
pact_hh/decision_injector.py
──────────────────────────────
DecisionInjector — re-injects a human's decision back into the PACT
coordination ecosystem.

What it does
────────────
  1. Publishes HUMAN_DECISION to the CoordinationBus
     → every subscribed pact-ax agent sees it immediately
  2. Updates TrustNetwork
     → agents whose votes matched the human decision get a trust boost
     → agents that disagreed get recalibrated
  3. Closes the EscalationStore record
     → SLA timers stop, record is archived
  4. Returns a typed InjectionResult with full audit trail

This is the "closing of the loop". Without this step, the human's response
lives in a Slack thread and never reaches the agents that need to learn.

Usage
─────
    injector = DecisionInjector(
        store = escalation_store,
        bus   = coordination_bus,       # optional — from pact-ax
        trust = trust_network,          # optional — from pact-ax
    )

    result = injector.inject(human_decision)
    # → InjectionResult(published=True, trust_updated=True, ...)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from pact_hh.escalation_packet import HumanDecision
from pact_hh.escalation_store import EscalationRecord, EscalationStore

logger = logging.getLogger(__name__)

# Try to import pact-ax types — graceful degradation if not installed
try:
    from pact_ax.coordination.coordination_bus import CoordinationBus, EventType
    _BUS_AVAILABLE = True
except ImportError:
    CoordinationBus = None
    EventType = None
    _BUS_AVAILABLE = False

try:
    from pact_ax.trust import TrustNetwork
    _TRUST_AVAILABLE = True
except ImportError:
    TrustNetwork = None
    _TRUST_AVAILABLE = False


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class InjectionResult:
    """Audit record of a single decision injection."""
    escalation_id:    str
    human_id:         str
    decision:         str
    published_to_bus: bool              = False
    trust_updated:    bool              = False
    store_closed:     bool              = False
    agents_notified:  List[str]         = field(default_factory=list)
    trust_deltas:     Dict[str, float]  = field(default_factory=dict)
    errors:           List[str]         = field(default_factory=list)
    injected_at:      datetime          = field(default_factory=datetime.utcnow)

    @property
    def success(self) -> bool:
        return self.published_to_bus or self.store_closed

    def __repr__(self) -> str:
        status = "✓" if self.success else "✗"
        return (
            f"InjectionResult({status} {self.escalation_id!r} "
            f"decision={self.decision!r} "
            f"bus={self.published_to_bus} trust={self.trust_updated})"
        )


# ── Trust calibration constants ───────────────────────────────────────────────

_TRUST_BOOST    = 0.02   # agent agreed with human
_TRUST_PENALTY  = 0.01   # agent disagreed with human


class DecisionInjector:
    """
    Re-injects a human decision back into the PACT coordination ecosystem.

    Parameters
    ----------
    store  : EscalationStore — used to close the record.
    bus    : CoordinationBus from pact-ax (optional).
             If None, injection is store-only (useful in tests / standalone).
    trust  : TrustNetwork from pact-ax (optional).
             If None, trust updates are skipped.
    """

    def __init__(
        self,
        store: EscalationStore,
        bus:   Optional[Any] = None,   # CoordinationBus | None
        trust: Optional[Any] = None,   # TrustNetwork | None
    ) -> None:
        self._store = store
        self._bus   = bus
        self._trust = trust

    # ── Main entry point ───────────────────────────────────────────────────────

    def inject(self, decision: HumanDecision) -> InjectionResult:
        """
        Full injection pipeline for a human decision.

        Steps
        ─────
        1. Look up the open escalation record in the store.
        2. Close the record (RESPONDED).
        3. Publish HUMAN_DECISION to the CoordinationBus.
        4. Update TrustNetwork based on agent vote alignment.

        Returns InjectionResult with audit details.
        """
        result = InjectionResult(
            escalation_id = decision.escalation_id,
            human_id      = decision.human_id,
            decision      = decision.decision,
        )

        # ── 1. Close the store record ─────────────────────────────────────────
        record = self._close_record(decision, result)

        # ── 2. Publish to CoordinationBus ─────────────────────────────────────
        if self._bus is not None:
            self._publish_to_bus(decision, record, result)

        # ── 3. Update TrustNetwork ────────────────────────────────────────────
        if self._trust is not None and record is not None:
            self._update_trust(decision, record, result)

        logger.info(
            "Injection complete: escalation=%s decision=%r "
            "bus=%s trust=%s store=%s",
            decision.escalation_id, decision.decision,
            result.published_to_bus, result.trust_updated, result.store_closed,
        )
        return result

    # ── Step implementations ───────────────────────────────────────────────────

    def _close_record(
        self,
        decision: HumanDecision,
        result:   InjectionResult,
    ) -> Optional[EscalationRecord]:
        try:
            record = self._store.resolve(decision.escalation_id, decision)
            result.store_closed = True
            return record
        except KeyError:
            msg = f"Escalation {decision.escalation_id!r} not found in store (already closed?)"
            logger.warning(msg)
            result.errors.append(msg)
            return None
        except Exception as exc:
            msg = f"Failed to close store record: {exc}"
            logger.error(msg)
            result.errors.append(msg)
            return None

    def _publish_to_bus(
        self,
        decision: HumanDecision,
        record:   Optional[EscalationRecord],
        result:   InjectionResult,
    ) -> None:
        """Publish a HUMAN_DECISION event so all pact-ax agents see the outcome."""
        if not _BUS_AVAILABLE:
            logger.debug("CoordinationBus not available — skipping bus publish")
            return

        try:
            payload = decision.to_dict()
            if record and record.packet:
                payload["original_intent"]   = record.packet.intent
                payload["session_id"]        = record.packet.session_id
                payload["agent_votes"]       = [
                    {"agent_id": v.agent_id, "decision": v.decision}
                    for v in record.packet.agent_votes
                ]

            self._bus.publish(
                EventType.HUMAN_DECISION,
                source  = "pact-hh",
                payload = payload,
            )
            result.published_to_bus = True

            # Who did we notify?
            result.agents_notified = [
                sub for sub in getattr(self._bus, "_subscriptions", {}).get(
                    EventType.HUMAN_DECISION, []
                )
            ]

        except Exception as exc:
            msg = f"Bus publish failed: {exc}"
            logger.error(msg)
            result.errors.append(msg)

    def _update_trust(
        self,
        decision: HumanDecision,
        record:   EscalationRecord,
        result:   InjectionResult,
    ) -> None:
        """
        Adjust agent trust scores based on how well they predicted the human decision.
        Agents that voted the same way get a small boost; dissenters get recalibrated.
        """
        if not record or not record.packet.agent_votes:
            return

        try:
            deltas: Dict[str, float] = {}
            for vote in record.packet.agent_votes:
                agreed = vote.decision.lower() == decision.decision.lower()
                delta  = _TRUST_BOOST if agreed else -_TRUST_PENALTY
                self._trust.update(vote.agent_id, delta)
                deltas[vote.agent_id] = delta
                logger.debug(
                    "Trust update: %s %+.3f (voted=%r, human=%r)",
                    vote.agent_id, delta, vote.decision, decision.decision,
                )

            result.trust_updated = True
            result.trust_deltas  = deltas

        except Exception as exc:
            msg = f"Trust update failed: {exc}"
            logger.error(msg)
            result.errors.append(msg)

    # ── Batch injection ───────────────────────────────────────────────────────

    def inject_many(self, decisions: List[HumanDecision]) -> List[InjectionResult]:
        """Inject multiple decisions. Returns results in the same order."""
        return [self.inject(d) for d in decisions]

    # ── Standalone helper (no pact-ax required) ───────────────────────────────

    @classmethod
    def create_standalone(cls, store: EscalationStore) -> "DecisionInjector":
        """
        Create an injector with no bus or trust network.
        Useful in tests or when running pact-hh without the full pact-ax stack.
        """
        return cls(store=store, bus=None, trust=None)
