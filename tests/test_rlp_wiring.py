"""
tests/test_rlp_wiring.py
─────────────────────────
Tests for rlp-0 wiring into DecisionInjector and HumanEscalationLoop.

Covers:
  - DecisionInjector: rlp_adapter param, InjectionResult.rlp_updated, _update_rlp()
  - HumanEscalationLoop: rlp_store param, on_escalation_opened signal on dispatch,
    rlp_adapter forwarded to injector, health/stats include rlp key
  - Full repair path: escalation → human approve → gate releases
  - Graceful degradation when rlp_store/rlp_adapter is None
"""

import pytest

from pact_hh.decision_injector import DecisionInjector, InjectionResult
from pact_hh.escalation_packet import (
    AgentVote, EscalationPacket, EscalationTrigger, HumanDecision,
)
from pact_hh.escalation_store import EscalationStore
from pact_hh.loop import HumanEscalationLoop, LoopConfig
from pact_hh.rlp_adapter import RLPAdapter


# ─── Stubs ────────────────────────────────────────────────────────────────────

class StubRLPSession:
    """Minimal RLPSession stub — tracks calls, controls gate_open."""

    def __init__(self, gate_open: bool = True):
        self._gate_open = gate_open
        self.decisions_received = []
        self.escalations_opened = []
        self._risk = 0.1 if gate_open else 0.8

    def on_human_decision(self, decision: str, agent_aligned: bool = True) -> None:
        self.decisions_received.append({"decision": decision, "agent_aligned": agent_aligned})

    def on_escalation_to_human(self) -> None:
        self.escalations_opened.append(True)

    def gate_open(self) -> bool:
        return self._gate_open

    def rupture_risk(self) -> float:
        return self._risk

    def status(self) -> dict:
        return {"session_id": "stub", "gate_open": self._gate_open}


class StubRLPSessionStore:
    """RLPSessionStore stub — returns configurable StubRLPSession instances."""

    def __init__(self, default_gate_open: bool = True):
        self._sessions: dict = {}
        self._default_gate_open = default_gate_open
        self.rupture_threshold = 0.45

    def get_or_create(self, session_id: str, **kwargs) -> StubRLPSession:
        if session_id not in self._sessions:
            self._sessions[session_id] = StubRLPSession(gate_open=self._default_gate_open)
        return self._sessions[session_id]

    def get(self, session_id: str):
        return self._sessions.get(session_id)

    def metrics(self) -> dict:
        return {"active_rlp_sessions": len(self._sessions), "avg_rupture_risk": 0.1, "gated_sessions": 0}


def _make_store() -> EscalationStore:
    return EscalationStore()


def _make_packet(session_id: str = "sess-1", recommended: str = "approve") -> EscalationPacket:
    return EscalationPacket(
        trigger      = EscalationTrigger.CONSENSUS_FAILED,
        intent       = "approve.refund",
        session_id   = session_id,
        recommended  = recommended,
        agent_votes  = [AgentVote(agent_id="agent-a", decision="approve", confidence=0.8)],
    )


def _open_packet(store: EscalationStore, packet: EscalationPacket) -> None:
    store.open(packet=packet, assigned_to="on-call", channel="slack")


def _make_decision(escalation_id: str, decision: str = "approve") -> HumanDecision:
    return HumanDecision(
        escalation_id = escalation_id,
        human_id      = "human-1",
        decision      = decision,
    )


# ─── InjectionResult ─────────────────────────────────────────────────────────

class TestInjectionResult:
    def test_rlp_updated_field_defaults_false(self):
        r = InjectionResult(escalation_id="e1", human_id="h1", decision="approve")
        assert r.rlp_updated is False

    def test_rlp_updated_can_be_set_true(self):
        r = InjectionResult(escalation_id="e1", human_id="h1", decision="approve", rlp_updated=True)
        assert r.rlp_updated is True

    def test_success_property_unchanged(self):
        r = InjectionResult(escalation_id="e1", human_id="h1", decision="approve",
                            published_to_bus=True)
        assert r.success is True


# ─── DecisionInjector ────────────────────────────────────────────────────────

class TestDecisionInjectorRLPWiring:
    def test_rlp_adapter_param_accepted(self):
        store   = _make_store()
        adapter = RLPAdapter(rlp_store=None)   # no store — safe no-op
        inj     = DecisionInjector(store=store, rlp_adapter=adapter)
        assert inj._rlp_adapter is adapter

    def test_rlp_adapter_defaults_none(self):
        inj = DecisionInjector(store=_make_store())
        assert inj._rlp_adapter is None

    def test_no_rlp_adapter_does_not_error(self):
        store  = _make_store()
        inj    = DecisionInjector(store=store)
        packet = _make_packet()
        _open_packet(store, packet)
        result = inj.inject(_make_decision(packet.escalation_id))
        assert result.rlp_updated is False
        assert result.store_closed is True

    def test_rlp_updated_true_when_adapter_finds_session(self):
        store      = _make_store()
        rlp_store  = StubRLPSessionStore()
        adapter    = RLPAdapter(rlp_store=rlp_store)

        packet     = _make_packet(session_id="sess-rlp")
        # Pre-create rlp session so the adapter can find it
        rlp_store.get_or_create("sess-rlp")
        _open_packet(store, packet)

        inj    = DecisionInjector(store=store, rlp_adapter=adapter)
        result = inj.inject(_make_decision(packet.escalation_id, decision="approve"))

        assert result.rlp_updated is True

    def test_rlp_updated_false_when_session_not_found(self):
        store   = _make_store()
        adapter = RLPAdapter(rlp_store=StubRLPSessionStore())  # empty store, no session yet

        packet  = _make_packet(session_id="no-such-session")
        _open_packet(store, packet)

        inj    = DecisionInjector(store=store, rlp_adapter=adapter)
        result = inj.inject(_make_decision(packet.escalation_id))

        # No rlp session pre-created → adapter returns False
        assert result.rlp_updated is False
        assert result.errors == []  # not an error, just a miss

    def test_human_decision_forwarded_to_rlp_session(self):
        store     = _make_store()
        rlp_store = StubRLPSessionStore()
        session   = rlp_store.get_or_create("sess-fwd")
        adapter   = RLPAdapter(rlp_store=rlp_store)

        packet    = _make_packet(session_id="sess-fwd", recommended="approve")
        _open_packet(store, packet)
        inj       = DecisionInjector(store=store, rlp_adapter=adapter)
        inj.inject(_make_decision(packet.escalation_id, decision="approve"))

        assert len(session.decisions_received) == 1
        assert session.decisions_received[0]["decision"] == "approve"

    def test_agent_alignment_detected_correctly(self):
        store     = _make_store()
        rlp_store = StubRLPSessionStore()
        session   = rlp_store.get_or_create("sess-align")
        adapter   = RLPAdapter(rlp_store=rlp_store)

        # packet.recommended = "approve", human decision = "approve" → aligned
        packet = _make_packet(session_id="sess-align", recommended="approve")
        _open_packet(store, packet)
        DecisionInjector(store=store, rlp_adapter=adapter).inject(
            _make_decision(packet.escalation_id, decision="approve")
        )
        assert session.decisions_received[0]["agent_aligned"] is True

    def test_agent_misalignment_detected_correctly(self):
        store     = _make_store()
        rlp_store = StubRLPSessionStore()
        session   = rlp_store.get_or_create("sess-mis")
        adapter   = RLPAdapter(rlp_store=rlp_store)

        # packet.recommended = "approve", human decision = "hold" → NOT aligned
        packet = _make_packet(session_id="sess-mis", recommended="approve")
        _open_packet(store, packet)
        DecisionInjector(store=store, rlp_adapter=adapter).inject(
            _make_decision(packet.escalation_id, decision="hold")
        )
        assert session.decisions_received[0]["agent_aligned"] is False

    def test_hold_decision_forwarded(self):
        store     = _make_store()
        rlp_store = StubRLPSessionStore()
        session   = rlp_store.get_or_create("sess-hold")
        adapter   = RLPAdapter(rlp_store=rlp_store)

        packet = _make_packet(session_id="sess-hold")
        _open_packet(store, packet)
        DecisionInjector(store=store, rlp_adapter=adapter).inject(
            _make_decision(packet.escalation_id, decision="hold")
        )
        assert session.decisions_received[0]["decision"] == "hold"

    def test_escalate_decision_forwarded(self):
        store     = _make_store()
        rlp_store = StubRLPSessionStore()
        session   = rlp_store.get_or_create("sess-esc")
        adapter   = RLPAdapter(rlp_store=rlp_store)

        packet = _make_packet(session_id="sess-esc")
        _open_packet(store, packet)
        DecisionInjector(store=store, rlp_adapter=adapter).inject(
            _make_decision(packet.escalation_id, decision="escalate")
        )
        assert session.decisions_received[0]["decision"] == "escalate"

    def test_rlp_update_after_trust_update_in_result(self):
        store     = _make_store()
        rlp_store = StubRLPSessionStore()
        rlp_store.get_or_create("sess-order")
        adapter   = RLPAdapter(rlp_store=rlp_store)

        packet = _make_packet(session_id="sess-order")
        _open_packet(store, packet)
        result = DecisionInjector(store=store, rlp_adapter=adapter).inject(
            _make_decision(packet.escalation_id)
        )
        # store_closed happens before rlp_updated — both should be True
        assert result.store_closed is True
        assert result.rlp_updated is True


# ─── HumanEscalationLoop ─────────────────────────────────────────────────────

class TestHumanEscalationLoopRLPWiring:
    def _make_loop(self, rlp_store=None):
        return HumanEscalationLoop(
            config    = LoopConfig(default_channel="slack"),
            rlp_store = rlp_store,
        )

    def test_rlp_adapter_none_when_no_store(self):
        loop = self._make_loop(rlp_store=None)
        assert loop._rlp_adapter is None

    def test_rlp_adapter_created_when_store_provided(self):
        store = StubRLPSessionStore()
        loop  = self._make_loop(rlp_store=store)
        assert loop._rlp_adapter is not None

    def test_rlp_adapter_forwarded_to_injector(self):
        store = StubRLPSessionStore()
        loop  = self._make_loop(rlp_store=store)
        assert loop._injector._rlp_adapter is loop._rlp_adapter

    def test_escalation_opened_fires_on_dispatch(self):
        rlp_store = StubRLPSessionStore()
        session   = rlp_store.get_or_create("sess-disp")
        loop      = self._make_loop(rlp_store=rlp_store)

        loop.escalate(
            trigger    = EscalationTrigger.CONSENSUS_FAILED,
            intent     = "approve.refund",
            session_id = "sess-disp",
        )
        # on_escalation_opened should have been called once
        assert len(session.escalations_opened) == 1

    def test_escalation_no_rlp_does_not_raise(self):
        loop = self._make_loop(rlp_store=None)
        outcome = loop.escalate(
            trigger    = EscalationTrigger.MANUAL,
            intent     = "any.intent",
            session_id = "no-rlp-session",
        )
        assert outcome.escalation_id is not None

    def test_create_factory_accepts_rlp_store(self):
        store = StubRLPSessionStore()
        loop  = HumanEscalationLoop.create(
            default_human_id = "on-call",
            rlp_store        = store,
            dry_run          = True,
        )
        assert loop._rlp_adapter is not None

    def test_create_factory_without_rlp_store(self):
        loop = HumanEscalationLoop.create(dry_run=True)
        assert loop._rlp_adapter is None

    def test_stats_includes_rlp_key(self):
        loop  = self._make_loop()
        stats = loop.stats()
        assert "rlp" in stats
        assert stats["rlp"] == "not connected"

    def test_stats_rlp_connected_when_store_provided(self):
        loop  = self._make_loop(rlp_store=StubRLPSessionStore())
        stats = loop.stats()
        assert stats["rlp"] == "connected"

    def test_health_includes_rlp_adapter_key(self):
        loop   = self._make_loop()
        health = loop.health()
        assert "rlp_adapter" in health
        assert health["rlp_adapter"] == "not connected"

    def test_health_rlp_adapter_connected_when_store_provided(self):
        loop   = self._make_loop(rlp_store=StubRLPSessionStore())
        health = loop.health()
        assert health["rlp_adapter"] == "connected"


# ─── Full repair path ─────────────────────────────────────────────────────────

class TestFullRepairPath:
    """
    End-to-end: escalation → human approve → rlp-0 repair signal emitted.
    Uses real RLPAdapter with stub store so we verify the call chain
    without needing rlp-0 installed.
    """

    def test_approve_triggers_rlp_repair(self):
        store      = _make_store()
        rlp_store  = StubRLPSessionStore()
        session    = rlp_store.get_or_create("full-sess")
        adapter    = RLPAdapter(rlp_store=rlp_store)
        inj        = DecisionInjector(store=store, rlp_adapter=adapter)

        packet = _make_packet(session_id="full-sess", recommended="approve")
        _open_packet(store, packet)
        result = inj.inject(_make_decision(packet.escalation_id, decision="approve"))

        assert result.rlp_updated is True
        assert len(session.decisions_received) == 1
        assert session.decisions_received[0]["decision"] == "approve"

    def test_loop_escalate_then_handle_reply(self):
        rlp_store = StubRLPSessionStore()
        rlp_store.get_or_create("loop-sess")  # pre-create so adapter can find it
        loop      = HumanEscalationLoop(
            config    = LoopConfig(default_channel="slack"),
            rlp_store = rlp_store,
        )

        # 1. Escalate — fires on_escalation_opened
        outcome = loop.escalate(
            trigger    = EscalationTrigger.CONSENSUS_FAILED,
            intent     = "approve.refund",
            session_id = "loop-sess",
            recommended = "approve",
        )

        # 2. Simulate human reply
        result = loop.handle_text_reply(
            text          = "approve",
            escalation_id = outcome.escalation_id,
            human_id      = "on-call",
        )

        assert result is not None
        assert result.store_closed is True
        # rlp_updated: True because rlp_store has the session
        assert result.rlp_updated is True

        # Both escalation_opened and human_decision should have been called
        session = rlp_store.get("loop-sess")
        assert len(session.escalations_opened) == 1
        assert len(session.decisions_received) == 1
        assert session.decisions_received[0]["decision"] == "approve"
