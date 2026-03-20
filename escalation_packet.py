"""
pact_hh/escalation_packet.py
──────────────────────────────
Core data types for the human escalation loop.

EscalationPacket  — structured representation of what happened + why
HumanDecision     — what the human decided (parsed from their response)
EscalationStatus  — lifecycle state of one escalation
EscalationTrigger — where the signal came from
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class EscalationStatus(str, Enum):
    PENDING          = "pending"           # waiting for human response
    RESPONDED        = "responded"         # human replied, decision injected
    TIMED_OUT        = "timed_out"         # SLA breached, no response
    ESCALATED_FURTHER = "escalated_further" # routed to a different human
    CANCELLED        = "cancelled"         # escalation no longer needed


class EscalationTrigger(str, Enum):
    CONSENSUS_FAILED    = "consensus_failed"     # agents couldn't agree
    POLICY_VIOLATED     = "policy_violated"      # safety constraint hit
    LOW_CONFIDENCE      = "low_confidence"       # no agent confident enough
    MANUAL              = "manual"               # explicitly requested


@dataclass
class AgentVote:
    """One agent's position, as shown to the human."""
    agent_id:   str
    decision:   str
    confidence: float
    reasoning:  str = ""

    def label(self) -> str:
        bar = "█" * int(self.confidence * 10) + "░" * (10 - int(self.confidence * 10))
        return f"{self.agent_id:<20} → {self.decision:<12} {bar} {self.confidence:.0%}  {self.reasoning}"


@dataclass
class EscalationPacket:
    """
    Everything pact-hh knows about the situation — structured for machines,
    readable for humans (once pact-hx renders it).

    Parameters
    ----------
    escalation_id : str        auto-generated
    trigger       : EscalationTrigger
    intent        : str        the PACT intent that caused the deadlock
    session_id    : str        originating conversation session
    agent_votes   : list       each agent's vote + confidence + reasoning
    context       : dict       full session context from pact-bridge
    recommended   : str        highest-weight option (bridge's suggestion)
    consensus_outcome : str    e.g. "DEADLOCK", "ESCALATE_TO_HUMAN"
    winning_weight : float     how close the best option came to passing
    threshold     : float      what weight was needed to pass
    sla_minutes   : int        how long the human has to respond
    metadata      : dict       arbitrary extra data
    """

    trigger:           EscalationTrigger
    intent:            str
    session_id:        str
    agent_votes:       List[AgentVote]      = field(default_factory=list)
    context:           Dict[str, Any]       = field(default_factory=dict)
    recommended:       Optional[str]        = None
    consensus_outcome: str                  = ""
    winning_weight:    float                = 0.0
    threshold:         float                = 0.0
    sla_minutes:       int                  = 30
    metadata:          Dict[str, Any]       = field(default_factory=dict)
    escalation_id:     str                  = field(default_factory=lambda: f"esc-{uuid.uuid4().hex[:10]}")
    created_at:        datetime             = field(default_factory=datetime.utcnow)

    # ── derived helpers ───────────────────────────────────────────────────────

    def unique_decisions(self) -> List[str]:
        seen, out = set(), []
        for v in self.agent_votes:
            if v.decision not in seen:
                seen.add(v.decision)
                out.append(v.decision)
        return out

    def vote_summary(self) -> Dict[str, float]:
        """decision → total confidence weight"""
        totals: Dict[str, float] = {}
        for v in self.agent_votes:
            totals[v.decision] = totals.get(v.decision, 0.0) + v.confidence
        return dict(sorted(totals.items(), key=lambda x: x[1], reverse=True))

    def plain_text(self) -> str:
        """
        Minimal plain-text representation — used by channels that don't
        support rich formatting, and as input to pact-hx for rendering.
        """
        lines = [
            f"DECISION REQUIRED — {self.intent}",
            f"Session: {self.session_id}",
            f"Trigger: {self.trigger.value}",
            "",
            "Agent votes:",
        ]
        for v in self.agent_votes:
            lines.append(f"  {v.label()}")

        if self.consensus_outcome:
            lines.append("")
            lines.append(f"Result: {self.consensus_outcome}")
            lines.append(
                f"Closest option: {self.winning_weight:.0%} weight "
                f"(needed {self.threshold:.0%})"
            )

        if self.recommended:
            lines.append("")
            lines.append(f"Recommended: {self.recommended.upper()}")

        lines += [
            "",
            f"SLA: respond within {self.sla_minutes} minutes",
            f"ID: {self.escalation_id}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "escalation_id":     self.escalation_id,
            "trigger":           self.trigger.value,
            "intent":            self.intent,
            "session_id":        self.session_id,
            "agent_votes":       [
                {"agent_id": v.agent_id, "decision": v.decision,
                 "confidence": v.confidence, "reasoning": v.reasoning}
                for v in self.agent_votes
            ],
            "recommended":       self.recommended,
            "consensus_outcome": self.consensus_outcome,
            "winning_weight":    round(self.winning_weight, 4),
            "threshold":         round(self.threshold, 4),
            "sla_minutes":       self.sla_minutes,
            "created_at":        self.created_at.isoformat(),
            "context":           self.context,
            "metadata":          self.metadata,
        }

    @classmethod
    def from_consensus_result(
        cls,
        result,                          # pact_ax ConsensusResult
        session_id:  str,
        context:     Dict[str, Any],
        sla_minutes: int = 30,
    ) -> "EscalationPacket":
        """Build an EscalationPacket directly from a pact-ax ConsensusResult."""
        votes = []
        for decision, agents in result.dissent_map.items():
            weight = result.vote_breakdown.get(decision, 0.0)
            avg_conf = weight / len(agents) if agents else 0.0
            for agent_id in agents:
                votes.append(AgentVote(
                    agent_id   = agent_id,
                    decision   = decision,
                    confidence = round(avg_conf, 3),
                    reasoning  = "",
                ))

        return cls(
            trigger           = EscalationTrigger.CONSENSUS_FAILED,
            intent            = context.get("last_intent", "unknown"),
            session_id        = session_id,
            agent_votes       = votes,
            context           = context,
            recommended       = result.winning_decision,
            consensus_outcome = result.outcome.value,
            winning_weight    = result.winning_weight / result.total_weight
                                if result.total_weight > 0 else 0.0,
            threshold         = 0.5,
            sla_minutes       = sla_minutes,
        )


@dataclass
class HumanDecision:
    """
    The structured outcome of a human's response to an escalation.

    Created by HumanResponseAdapter after parsing the raw reply.
    Injected into CoordinationBus by DecisionInjector.
    """

    escalation_id: str
    human_id:      str
    decision:      str                  # "approve" | "hold" | "escalate" | custom
    reasoning:     str                 = ""
    confidence:    float               = 0.95   # humans default high
    channel:       str                 = "unknown"
    raw_response:  str                 = ""
    responded_at:  datetime            = field(default_factory=datetime.utcnow)
    metadata:      Dict[str, Any]      = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "escalation_id": self.escalation_id,
            "human_id":      self.human_id,
            "decision":      self.decision,
            "reasoning":     self.reasoning,
            "confidence":    self.confidence,
            "channel":       self.channel,
            "responded_at":  self.responded_at.isoformat(),
        }
