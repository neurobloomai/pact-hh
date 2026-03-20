"""
pact_hh/human_channels/base.py
────────────────────────────────
HumanChannel — abstract base for all delivery channels.

Every channel must:
  1. send(packet, assignment) → send the escalation to the human
  2. receive(raw)             → parse the human's raw response
  3. health()                 → is this channel currently usable?

Channels are registered by name on the ChannelRegistry and looked up
at runtime by EscalationRouter's channel field ("slack", "email", "webhook").
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from pact_hh.escalation_packet import EscalationPacket, HumanDecision
from pact_hh.escalation_router import RoutingAssignment

logger = logging.getLogger(__name__)


# ── What the channel sends back after delivery ────────────────────────────────

@dataclass
class DeliveryReceipt:
    """Proof that a message was dispatched to the channel."""
    escalation_id: str
    channel:       str
    delivered:     bool
    message_id:    Optional[str]  = None   # Slack ts, email Message-ID, etc.
    error:         Optional[str]  = None
    metadata:      Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.delivered


# ── Abstract channel interface ────────────────────────────────────────────────

class HumanChannel(ABC):
    """
    Base class for all delivery channels.

    Subclasses must implement:
      - send()    — deliver EscalationPacket to the human
      - receive() — parse a raw inbound message into HumanDecision
      - health()  — return True if the channel is operational
    """

    name: str = "base"

    # ── send / receive ────────────────────────────────────────────────────────

    @abstractmethod
    def send(
        self,
        packet:     EscalationPacket,
        assignment: RoutingAssignment,
        body:       str,           # pre-rendered text/blocks from pact-hx
    ) -> DeliveryReceipt:
        """
        Deliver *packet* to the assigned human via this channel.

        Parameters
        ----------
        packet     : The raw escalation data.
        assignment : Who to send it to, and via which channel.
        body       : Rendered message body (plain text or channel-native format).
                     In the full stack this is produced by pact-hx.
                     In standalone mode, packet.plain_text() is used as fallback.

        Returns
        -------
        DeliveryReceipt indicating success or failure.
        """

    @abstractmethod
    def receive(self, raw: Dict[str, Any]) -> Optional[HumanDecision]:
        """
        Parse a raw inbound event from this channel into a HumanDecision.

        Returns None if the event is not a valid escalation response.
        """

    @abstractmethod
    def health(self) -> bool:
        """Return True if this channel is currently reachable."""

    # ── helpers ───────────────────────────────────────────────────────────────

    def _safe_send(
        self,
        packet:     EscalationPacket,
        assignment: RoutingAssignment,
        body:       str,
    ) -> DeliveryReceipt:
        """Wraps send() with error handling — always returns a receipt."""
        try:
            return self.send(packet, assignment, body)
        except Exception as exc:
            logger.error(
                "Channel %r failed to deliver escalation %s: %s",
                self.name, packet.escalation_id, exc,
            )
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = False,
                error         = str(exc),
            )

    def render_fallback(self, packet: EscalationPacket) -> str:
        """Plain-text render used when pact-hx is not available."""
        return packet.plain_text()


# ── Channel registry ──────────────────────────────────────────────────────────

class ChannelRegistry:
    """
    Global registry of available HumanChannel implementations.

    Usage
    ─────
        registry = ChannelRegistry()
        registry.register(SlackChannel(bot_token=...))
        channel  = registry.get("slack")
        receipt  = channel.send(packet, assignment, body)
    """

    def __init__(self) -> None:
        self._channels: Dict[str, HumanChannel] = {}

    def register(self, channel: HumanChannel) -> None:
        self._channels[channel.name] = channel
        logger.info("Registered channel: %r", channel.name)

    def get(self, name: str) -> HumanChannel:
        if name not in self._channels:
            raise KeyError(
                f"No channel registered for {name!r}. "
                f"Available: {list(self._channels)}"
            )
        return self._channels[name]

    def available(self) -> list:
        return list(self._channels)

    def healthy(self) -> Dict[str, bool]:
        return {name: ch.health() for name, ch in self._channels.items()}

    def __repr__(self) -> str:
        return f"ChannelRegistry({list(self._channels)})"


# ── Shared default registry ───────────────────────────────────────────────────

_default_registry: Optional[ChannelRegistry] = None


def get_registry() -> ChannelRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = ChannelRegistry()
    return _default_registry
