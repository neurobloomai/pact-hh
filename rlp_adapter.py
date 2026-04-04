"""
pact_hh/rlp_adapter.py

pact-hh → rlp-0 adapter.

When a human decision is injected back into the system, this adapter:
  1. Looks up the RLPSession for the originating bridge session
  2. Calls on_human_decision() with the parsed outcome
  3. Lets rlp-0 determine whether repair is complete and the gate can release

Drop this file into pact_hh/ and call RLPAdapter.on_decision() from
decision_injector.py after publishing HUMAN_DECISION to the CoordinationBus.
(See INTEGRATION.md for the exact lines.)

Design note:
  pact-hh already updates TrustNetwork scores (agents who aligned +0.02,
  dissenters -0.01). This adapter handles the *relational* layer — rlp-0 —
  which is a different signal. TrustNetwork is epistemic (did the agent
  perform well?). rlp-0 is relational (is the relationship healthy?).
  Both matter. Neither replaces the other.
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pact_bridge.rlp_session import RLPSessionStore

logger = logging.getLogger(__name__)


class RLPAdapter:
    """
    Connects pact-hh decision outcomes to rlp-0 relational state.

    Usage:
        # In pact-hh HumanEscalationLoop or DecisionInjector:
        rlp_adapter = RLPAdapter(rlp_store=bridge.rlp_store)

        # After human decision is parsed and injected:
        rlp_adapter.on_decision(
            session_id=packet.session_id,
            decision=parsed_decision,       # 'approve' | 'hold' | 'escalate'
            agent_recommendation=packet.recommended_action,
        )
    """

    def __init__(self, rlp_store: Optional["RLPSessionStore"] = None):
        self._store = rlp_store
        self._available = rlp_store is not None

        if not self._available:
            logger.debug(
                "[RLPAdapter] no RLPSessionStore provided — "
                "human decisions will not update rlp-0 state"
            )

    def attach_store(self, rlp_store: "RLPSessionStore") -> "RLPAdapter":
        """Attach store post-construction. Chainable."""
        self._store = rlp_store
        self._available = True
        return self

    def on_decision(
        self,
        session_id: str,
        decision: str,
        agent_recommendation: Optional[str] = None,
    ) -> bool:
        """
        Called after a human decision is parsed and injected into the system.

        Args:
            session_id:           The bridge session this escalation originated from.
            decision:             Parsed human decision: 'approve' | 'hold' | 'escalate'
            agent_recommendation: What the agents recommended (for alignment detection).

        Returns:
            True if rlp-0 state was updated, False if session not found or rlp-0 unavailable.
        """
        if not self._available:
            return False

        rlp_session = self._store.get(session_id)
        if rlp_session is None:
            logger.debug(
                f"[RLPAdapter] no RLPSession found for session_id={session_id} — "
                "this is expected if the session predates rlp-0 integration"
            )
            return False

        # Determine whether agents were aligned with the human decision
        agent_aligned = (
            agent_recommendation is not None
            and agent_recommendation.lower() == decision.lower()
        )

        rlp_session.on_human_decision(
            decision=decision,
            agent_aligned=agent_aligned,
        )

        logger.info(
            f"[RLPAdapter] session={session_id} decision={decision} "
            f"agent_aligned={agent_aligned} "
            f"rupture_risk={rlp_session.rupture_risk():.2f} "
            f"gate_open={rlp_session.gate_open()}"
        )

        return True

    def on_escalation_opened(self, session_id: str) -> bool:
        """
        Called when a new escalation packet is routed to a human.
        Signals relational stress in the originating session.
        """
        if not self._available:
            return False

        rlp_session = self._store.get(session_id)
        if rlp_session is None:
            return False

        rlp_session.on_escalation_to_human()

        logger.debug(
            f"[RLPAdapter] escalation opened session={session_id} "
            f"rupture_risk={rlp_session.rupture_risk():.2f}"
        )
        return True

    def session_status(self, session_id: str) -> Optional[dict]:
        """Return rlp-0 status for a session. Useful for observability."""
        if not self._available:
            return None
        rlp_session = self._store.get(session_id)
        if rlp_session is None:
            return None
        return rlp_session.status()
