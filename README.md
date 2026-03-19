# pact-hh

> The Human-Human layer. Where AI agents stop, and humans step in.

---

## The problem with "escalate to human"

Every multi-agent system eventually reaches a moment where the agents can't agree, confidence is too low, or the stakes are too high to act autonomously. The standard response is to flag it for a human.

But flagging is not a loop. It's a dead end.

The human gets notified. They make a decision. And then what? That decision lives in an email, a Slack thread, a ticket. Nothing picks it up and feeds it back. The agents that escalated never learn. The next time the same situation comes up, they escalate again — to the same dead end.

**pact-hh closes the loop.**

It catches every `ESCALATE_TO_HUMAN` signal in the PACT ecosystem, routes it to the right person with full context, waits for their response, and re-injects that decision back into the coordination system — as a first-class event that agents learn from.

---

## Where escalations come from

Three places in the PACT stack fire `ESCALATE_TO_HUMAN`:

```
pact-ax ConsensusProtocol
  → agents voted but no decision crossed the threshold
  → too much disagreement to act safely

pact-ax PolicyAlignmentManager
  → a proposed action violates a safety-critical constraint
  → no agent has enough confidence to proceed

pact-ax HumilityAwareCoordinator
  → no agent has sufficient epistemic confidence for this query
  → the right move is to defer, not guess
```

All three fire a `CONSENSUS_FAILED` or `ESCALATION_TRIGGERED` event on the **CoordinationBus**. pact-hh subscribes to both.

---

## The loop

```
 pact-ax agents attempt to reach consensus
         │
         │  confidence too low / disagreement too sharp
         ▼
 ConsensusProtocol → ESCALATE_TO_HUMAN
         │
         ▼
 ┌─────────────────────────────────────────────────┐
 │                   pact-hh                        │
 │                                                  │
 │  EscalationRouter                                │
 │    └─ who is the right human for this?           │
 │    └─ what channel do they prefer?               │
 │                                                  │
 │  EscalationPacket (what the human sees)          │
 │    └─ what was being decided                     │
 │    └─ why agents couldn't agree                  │
 │    └─ each agent's vote + confidence + reasoning │
 │    └─ relevant context + session history         │
 │    └─ recommended action (highest-weight option) │
 │                                                  │
 │  HumanChannel                                    │
 │    └─ Slack / email / webhook / UI               │
 │                                                  │
 │  ← human responds: decision + optional reasoning │
 │                                                  │
 │  HumanResponseAdapter                            │
 │    └─ parse response into PACT decision          │
 │    └─ build EpistemicState from human confidence │
 │                                                  │
 │  re-inject into CoordinationBus                  │
 │    → HUMAN_DECISION event                        │
 │    → agents receive, update trust + knowledge    │
 │    → session resumes                             │
 └─────────────────────────────────────────────────┘
         │
         ▼
 pact-ax coordination resumes with human decision
 pact-bridge routes response back to external platform
```

The human is not an interruption. They are a participant in the coordination protocol.

---

## What the human actually sees

The `EscalationPacket` is designed for clarity, not noise. The human sees exactly what they need — nothing more:

```
─────────────────────────────────────────────────────
  🔔 Decision Required — High Confidence Needed
─────────────────────────────────────────────────────

  Context
  ───────
  Session:   sess-abc-001
  Intent:    approve_refund (£1,240 — Customer CUST-99)
  Initiated: 2 minutes ago by billing-agent

  Why agents couldn't decide
  ──────────────────────────
  billing-agent    → APPROVE   confidence 0.72  "within threshold but borderline"
  compliance-agent → HOLD      confidence 0.81  "exceeds weekly auto-approve limit"
  risk-agent       → APPROVE   confidence 0.65  "customer history is clean"

  Consensus strategy: weighted_vote
  Result: DEADLOCK (winning weight 0.54, threshold 0.60)

  Recommended action
  ──────────────────
  APPROVE  (2/3 agents, combined weight 0.68)

  ──────────────────────────────────────────────────
  Reply with one of:
    approve [optional reasoning]
    hold    [optional reasoning]
    escalate [name or team]
  ──────────────────────────────────────────────────
```

---

## What happens with the human's response

The human replies. pact-hh parses it into a structured `HumanDecision`:

```python
HumanDecision(
    decision    = "approve",
    reasoning   = "Customer has 3yr clean history. One-time exception.",
    confidence  = 0.95,   # derived from response certainty signals
    human_id    = "amarnath@neurobloom.ai",
    responded_at = datetime.utcnow(),
)
```

This is then:

1. **Published to CoordinationBus** as `EventType.HUMAN_DECISION` — every subscribed agent sees it
2. **Fed into TrustNetwork** — agents whose votes aligned with the human get a small trust boost; those who diverged get recalibrated
3. **Stored as EpistemicState** — future consensus rounds on similar intents use this as prior knowledge
4. **Routed back through pact-bridge** — the original external platform gets its response

The agents learn. The loop closes.

---

## Architecture

pact-hh sits between pact-ax coordination and the humans in your organisation:

```
┌──────────────────────────────────────────────────────────────┐
│                      PACT Ecosystem                           │
│                                                              │
│  ┌──────────┐    ┌──────────┐    ┌─────────────────────┐    │
│  │   pact   │    │  pact-ax │    │    pact-bridge       │    │
│  │ (intents)│◄──►│ (agents) │◄──►│  (orchestration)    │    │
│  └──────────┘    └────┬─────┘    └─────────────────────┘    │
│                       │                                       │
│              ESCALATE_TO_HUMAN                                │
│                       │                                       │
│                  ┌────▼─────┐                                │
│                  │ pact-hh  │  ◄── this repo                 │
│                  │          │                                │
│                  │ catches  │                                │
│                  │ routes   │                                │
│                  │ waits    │                                │
│                  │ re-injects│                               │
│                  └────┬─────┘                                │
│                       │                                       │
└───────────────────────┼──────────────────────────────────────┘
                        │
              ┌─────────▼──────────┐
              │   Human Channels    │
              │  Slack / Email /    │
              │  Webhook / UI       │
              └────────────────────┘
```

---

## Planned modules

```
pact_hh/
├── escalation_router.py     Who handles this escalation? Which channel?
├── escalation_packet.py     EscalationPacket — what the human sees
├── human_channels/
│   ├── base.py              HumanChannel interface
│   ├── slack.py             Slack blocks + response listener
│   ├── email.py             HTML email + reply parser
│   └── webhook.py           Generic HTTP webhook + callback
├── response_adapter.py      Parse human reply → HumanDecision
├── decision_injector.py     Re-inject into CoordinationBus + TrustNetwork
├── escalation_store.py      Track open escalations, timeouts, SLA
└── __init__.py
```

---

## Integration points

**Subscribes to** (from pact-ax CoordinationBus):
- `EventType.CONSENSUS_FAILED` — when `ConsensusOutcome.ESCALATE_TO_HUMAN`
- `EventType.ESCALATION_TRIGGERED` — from HumilityAwareCoordinator
- `EventType.POLICY_VIOLATED` — from PolicyAlignmentManager

**Publishes** (back to pact-ax CoordinationBus):
- `EventType.HUMAN_DECISION` — human's parsed response
- `EventType.TRUST_UPDATED` — recalibration signals for agents that voted

**Reads from** (for context):
- pact-bridge SessionStore — full conversation history
- pact-ax TrustNetwork — current agent trust scores
- pact-ax ConsensusResult — vote breakdown, confidence scores

---

## Design principles

**Context over noise.** The human receives exactly what they need to decide — not raw system state, not agent logs. The escalation packet is written for a person, not a developer.

**Decisions are knowledge.** A human decision is not just an answer. It's an EpistemicState update. Every agent in the network learns from it.

**The loop must close.** An escalation that doesn't return a decision is a failure of the protocol, not a feature. pact-hh tracks SLAs, sends reminders, and escalates further if the human doesn't respond within a configurable window.

**Humans are participants, not fallbacks.** The protocol treats human judgment as a first-class epistemic source — equal in weight to any agent, but with higher confidence assigned by default.

---

## Relationship to the PACT stack

| Repo | Role |
|------|------|
| [pact](https://github.com/neurobloomai/pact) | Intent translation across platforms |
| [pact-ax](https://github.com/neurobloomai/pact-ax) | Agent collaboration primitives |
| [pact-bridge](https://github.com/neurobloomai/pact-bridge) | Orchestration — connects pact + pact-ax |
| **pact-hh** | **Human escalation loop — closes the circle** |

The PACT ecosystem is only complete when human judgment can enter and exit the system as cleanly as agent judgment. pact-hh is that door.

---

## Status

🚧 **In active development.**

The architecture above reflects the design intent. Implementation is underway. If you want to contribute — channels, response parsers, or the decision injector — open an issue or reach out at founders@neurobloom.ai.

---

MIT License · [neurobloom.ai](https://neurobloom.ai)
