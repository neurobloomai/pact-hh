"""
pact_hh/escalation_router.py
──────────────────────────────
EscalationRouter — decides WHO handles an escalation and via WHICH channel.

Routing rules are configurable:
  - By intent pattern     ("approve_refund" → finance team)
  - By trigger type       (POLICY_VIOLATED → compliance officer)
  - By confidence floor   (< 0.4 → senior reviewer)
  - Default fallback      (catch-all human or team)

Usage
─────
    from pact_hh.escalation_router import EscalationRouter, RoutingRule

    router = EscalationRouter(
        rules=[
            RoutingRule(intent_pattern="approve_refund",  human_id="finance-lead",   channel="slack"),
            RoutingRule(intent_pattern="policy",          human_id="compliance",      channel="email"),
            RoutingRule(trigger="policy_violated",        human_id="legal-team",      channel="slack"),
        ],
        default_human_id  = "on-call-manager",
        default_channel   = "slack",
    )

    assignment = router.route(packet)
    # → RoutingAssignment(human_id="finance-lead", channel="slack", rule_matched="...")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from pact_hh.escalation_packet import EscalationPacket, EscalationTrigger

logger = logging.getLogger(__name__)


@dataclass
class RoutingRule:
    """
    A single routing rule. First matching rule wins.

    Parameters
    ----------
    human_id : str
        Who to assign the escalation to.
    channel : str
        Which channel to use — "slack", "email", "webhook".
    intent_pattern : str, optional
        Regex matched against the escalation's intent string.
    trigger : str, optional
        Match against EscalationTrigger value.
    min_votes : int, optional
        Only match if there are at least this many agent votes.
    max_confidence : float, optional
        Only match if the winning vote confidence is ≤ this value.
    priority : int
        Lower = higher priority. Default 100.
    label : str
        Human-readable description for logs/metrics.
    """

    human_id:        str
    channel:         str                    = "slack"
    intent_pattern:  Optional[str]          = None
    trigger:         Optional[str]          = None
    min_votes:       Optional[int]          = None
    max_confidence:  Optional[float]        = None
    priority:        int                    = 100
    label:           str                    = ""

    def matches(self, packet: EscalationPacket) -> bool:
        if self.intent_pattern:
            if not re.search(self.intent_pattern, packet.intent, re.IGNORECASE):
                return False
        if self.trigger:
            if packet.trigger.value != self.trigger:
                return False
        if self.min_votes is not None:
            if len(packet.agent_votes) < self.min_votes:
                return False
        if self.max_confidence is not None:
            top = max((v.confidence for v in packet.agent_votes), default=1.0)
            if top > self.max_confidence:
                return False
        return True


@dataclass
class RoutingAssignment:
    """Output of EscalationRouter.route()."""
    human_id:     str
    channel:      str
    rule_matched: str   = "default"
    fallback:     bool  = False

    def __repr__(self) -> str:
        tag = " [fallback]" if self.fallback else ""
        return f"RoutingAssignment({self.human_id!r} via {self.channel!r}{tag})"


class EscalationRouter:
    """
    Routes escalation packets to the right human + channel.

    Parameters
    ----------
    rules : list[RoutingRule]
        Evaluated in priority order. First match wins.
    default_human_id : str
        Fallback assignee when no rule matches.
    default_channel : str
        Fallback channel when no rule matches. Default "slack".
    """

    def __init__(
        self,
        rules:             List[RoutingRule] = None,
        default_human_id:  str               = "on-call",
        default_channel:   str               = "slack",
    ) -> None:
        self._rules           = sorted(rules or [], key=lambda r: r.priority)
        self._default_human   = default_human_id
        self._default_channel = default_channel
        self._stats: dict     = {"routed": 0, "fallback": 0}

    def route(self, packet: EscalationPacket) -> RoutingAssignment:
        """
        Find the best routing assignment for *packet*.
        Returns a RoutingAssignment — never raises.
        """
        for rule in self._rules:
            if rule.matches(packet):
                self._stats["routed"] += 1
                logger.info(
                    "Escalation %s → %s via %s (rule: %r)",
                    packet.escalation_id, rule.human_id, rule.channel,
                    rule.label or rule.intent_pattern or rule.trigger or "unnamed",
                )
                return RoutingAssignment(
                    human_id     = rule.human_id,
                    channel      = rule.channel,
                    rule_matched = rule.label or "rule",
                )

        # No match — use defaults
        self._stats["fallback"] += 1
        logger.warning(
            "No routing rule matched escalation %s (intent=%r) — using default %s",
            packet.escalation_id, packet.intent, self._default_human,
        )
        return RoutingAssignment(
            human_id     = self._default_human,
            channel      = self._default_channel,
            rule_matched = "default",
            fallback     = True,
        )

    def add_rule(self, rule: RoutingRule) -> None:
        """Add a rule at runtime and re-sort by priority."""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)

    def stats(self) -> dict:
        return dict(self._stats)

    def __repr__(self) -> str:
        return (
            f"EscalationRouter(rules={len(self._rules)}, "
            f"default={self._default_human!r})"
        )
