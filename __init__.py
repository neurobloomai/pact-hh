"""
pact-hh — Human escalation loop for the PACT ecosystem.

Closes the ESCALATE_TO_HUMAN dead end:
  escalation → routing → delivery → human reply → re-injection → agents learn

Quick start
───────────
    from pact_hh import HumanEscalationLoop

    loop = HumanEscalationLoop.create(
        slack_token      = "xoxb-...",
        default_human_id = "on-call-manager",
    )
    loop.start()

    # When a human replies via Slack:
    result = loop.handle_reply(slack_payload, channel="slack")
"""

__version__ = "0.1.0"

from pact_hh.decision_injector import DecisionInjector, InjectionResult
from pact_hh.escalation_packet import (
    AgentVote,
    EscalationPacket,
    EscalationStatus,
    EscalationTrigger,
    HumanDecision,
)
from pact_hh.escalation_router import EscalationRouter, RoutingAssignment, RoutingRule
from pact_hh.escalation_store import EscalationRecord, EscalationStore
from pact_hh.human_channels import (
    ChannelRegistry,
    DeliveryReceipt,
    EmailChannel,
    HumanChannel,
    SlackChannel,
    WebhookChannel,
    get_registry,
)
from pact_hh.loop import EscalationOutcome, HumanEscalationLoop, LoopConfig
from pact_hh.response_adapter import HumanResponseAdapter

__all__ = [
    # Core types
    "EscalationPacket",
    "EscalationStatus",
    "EscalationTrigger",
    "AgentVote",
    "HumanDecision",
    # Routing
    "EscalationRouter",
    "RoutingRule",
    "RoutingAssignment",
    # Store / SLA
    "EscalationStore",
    "EscalationRecord",
    # Channels
    "HumanChannel",
    "DeliveryReceipt",
    "ChannelRegistry",
    "get_registry",
    "SlackChannel",
    "EmailChannel",
    "WebhookChannel",
    # Parsing
    "HumanResponseAdapter",
    # Injection
    "DecisionInjector",
    "InjectionResult",
    # Main loop
    "HumanEscalationLoop",
    "LoopConfig",
    "EscalationOutcome",
]
