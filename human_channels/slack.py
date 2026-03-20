"""
pact_hh/human_channels/slack.py
─────────────────────────────────
SlackChannel — sends escalation packets as rich Slack Block Kit messages
and parses replies back into HumanDecision.

Usage
─────
    from pact_hh.human_channels.slack import SlackChannel

    channel = SlackChannel(
        bot_token     = "xoxb-...",
        signing_secret = "...",         # for webhook verification
        default_channel = "#escalations",
    )

    receipt = channel.send(packet, assignment, body)
    # → DeliveryReceipt(delivered=True, message_id="1234567890.123456")

Inbound (slash command / action payload):
    decision = channel.receive(slack_payload)
    # → HumanDecision(decision="approve", reasoning="...", ...)

No hard dependency on the slack_sdk. If it's not installed, SlackChannel
degrades to a dry-run stub that logs what it would have sent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional

from pact_hh.escalation_packet import EscalationPacket, HumanDecision
from pact_hh.escalation_router import RoutingAssignment
from pact_hh.human_channels.base import DeliveryReceipt, HumanChannel

logger = logging.getLogger(__name__)

# Try to import slack_sdk — degrade gracefully if missing
try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SLACK_SDK_AVAILABLE = True
except ImportError:
    _SLACK_SDK_AVAILABLE = False
    WebClient = None
    SlackApiError = Exception


class SlackChannel(HumanChannel):
    """
    Sends escalation packets to Slack and parses replies.

    Parameters
    ----------
    bot_token       : Slack Bot OAuth token (xoxb-...).
    signing_secret  : Used to verify incoming webhook signatures.
    default_channel : Fallback Slack channel ID/name if routing has no target.
    dry_run         : If True, log actions without actually calling Slack.
    """

    name = "slack"

    def __init__(
        self,
        bot_token:       str  = "",
        signing_secret:  str  = "",
        default_channel: str  = "#escalations",
        dry_run:         bool = False,
    ) -> None:
        self._token          = bot_token
        self._signing_secret = signing_secret
        self._default_channel = default_channel
        self._dry_run        = dry_run or not _SLACK_SDK_AVAILABLE
        self._client         = WebClient(token=bot_token) if (_SLACK_SDK_AVAILABLE and bot_token) else None

        if not _SLACK_SDK_AVAILABLE:
            logger.warning(
                "slack_sdk not installed — SlackChannel running in dry-run mode. "
                "Install with: pip install slack_sdk"
            )

    # ── HumanChannel interface ─────────────────────────────────────────────────

    def send(
        self,
        packet:     EscalationPacket,
        assignment: RoutingAssignment,
        body:       str,
    ) -> DeliveryReceipt:
        """Post the escalation as a Slack message with Block Kit formatting."""
        channel_target = self._resolve_target(assignment)
        blocks         = self._build_blocks(packet, body)

        if self._dry_run:
            logger.info(
                "[DRY RUN] SlackChannel.send → channel=%r, escalation=%s\n%s",
                channel_target, packet.escalation_id, body,
            )
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = True,
                message_id    = f"dry-run-{packet.escalation_id}",
            )

        try:
            resp = self._client.chat_postMessage(
                channel = channel_target,
                text    = f"🔔 Decision Required — {packet.intent}",
                blocks  = blocks,
            )
            ts = resp["ts"]
            logger.info(
                "Slack message sent for escalation %s → %s (ts=%s)",
                packet.escalation_id, channel_target, ts,
            )
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = True,
                message_id    = ts,
                metadata      = {"slack_channel": channel_target},
            )

        except SlackApiError as exc:
            logger.error("SlackApiError for escalation %s: %s", packet.escalation_id, exc)
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = False,
                error         = str(exc),
            )

    def receive(self, raw: Dict[str, Any]) -> Optional[HumanDecision]:
        """
        Parse a Slack Events API / slash command payload into a HumanDecision.

        Supported formats
        -----------------
        1. slash command: /approve <esc-id> [reasoning]
        2. Slack Actions block button click (action_id = "pact_hh_decision")
        3. Message reply containing "esc-<id>" mention
        """
        # ── slash command ──────────────────────────────────────────────────
        if raw.get("type") == "slash_command":
            return self._parse_slash(raw)

        # ── interactive component (button click) ──────────────────────────
        payload_str = raw.get("payload") or raw.get("body", {}).get("payload", "")
        if payload_str:
            try:
                payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
                if payload.get("type") == "block_actions":
                    return self._parse_block_action(payload)
            except (json.JSONDecodeError, KeyError):
                pass

        # ── plain message event containing escalation ID ──────────────────
        event = raw.get("event", {})
        if event.get("type") == "message":
            return self._parse_message_event(event)

        return None

    def health(self) -> bool:
        if self._dry_run:
            return True
        try:
            resp = self._client.auth_test()
            return bool(resp.get("ok"))
        except Exception:
            return False

    # ── block kit builder ──────────────────────────────────────────────────────

    def _build_blocks(self, packet: EscalationPacket, body: str) -> List[Dict]:
        """
        Construct a Slack Block Kit message from the escalation packet.
        Falls back to a plain text section if body is already rendered.
        """
        vote_lines = "\n".join(
            f"• *{v.agent_id}* → {v.decision} ({v.confidence:.0%}) — _{v.reasoning}_"
            for v in packet.agent_votes
        ) or "_No agent votes recorded_"

        recommended = (
            f"\n\n✅ *Recommended:* `{packet.recommended.upper()}`"
            if packet.recommended else ""
        )

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🔔 Decision Required — {packet.intent}"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Session:* `{packet.session_id}`\n"
                        f"*Trigger:* `{packet.trigger.value}`\n"
                        f"*SLA:* respond within *{packet.sla_minutes} minutes*\n"
                        f"*ID:* `{packet.escalation_id}`"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Agent votes:*\n{vote_lines}{recommended}"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Reply with one of:\n"
                        f"> `approve [reasoning]`\n"
                        f"> `hold [reasoning]`\n"
                        f"> `escalate [name or team]`\n\n"
                        f"Or use the buttons below:"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    self._button("✅ Approve", "approve", packet.escalation_id, "primary"),
                    self._button("⏸ Hold",    "hold",    packet.escalation_id, "danger"),
                    self._button("⬆ Escalate","escalate",packet.escalation_id),
                ],
            },
        ]
        return blocks

    @staticmethod
    def _button(text: str, value: str, escalation_id: str, style: str = "") -> Dict:
        btn: Dict[str, Any] = {
            "type":      "button",
            "text":      {"type": "plain_text", "text": text},
            "value":     f"{value}:{escalation_id}",
            "action_id": f"pact_hh_{value}",
        }
        if style:
            btn["style"] = style
        return btn

    # ── parsers ────────────────────────────────────────────────────────────────

    def _parse_slash(self, raw: Dict) -> Optional[HumanDecision]:
        """Parse /approve esc-abc123 Customer has clean history."""
        text    = raw.get("text", "").strip()
        user_id = raw.get("user_id", "unknown")

        parts = text.split(None, 2)   # [decision, esc-id, reasoning?]
        if len(parts) < 2:
            return None

        decision_word   = parts[0].lower()
        escalation_id   = parts[1]
        reasoning       = parts[2] if len(parts) > 2 else ""

        return HumanDecision(
            escalation_id = escalation_id,
            human_id      = user_id,
            decision      = decision_word,
            reasoning     = reasoning,
            channel       = self.name,
            raw_response  = text,
        )

    def _parse_block_action(self, payload: Dict) -> Optional[HumanDecision]:
        """Parse a block_actions payload from a button click."""
        user       = payload.get("user", {})
        actions    = payload.get("actions", [])
        if not actions:
            return None

        action    = actions[0]
        value     = action.get("value", "")     # "approve:esc-abc123"
        parts     = value.split(":", 1)
        if len(parts) != 2:
            return None

        decision_word, escalation_id = parts
        return HumanDecision(
            escalation_id = escalation_id,
            human_id      = user.get("id", "unknown"),
            decision      = decision_word,
            channel       = self.name,
            raw_response  = json.dumps(action),
        )

    def _parse_message_event(self, event: Dict) -> Optional[HumanDecision]:
        """
        Parse a free-form Slack message mentioning an escalation ID.
        Expected format: "approve esc-abc123 Customer has clean history"
        """
        text = event.get("text", "").strip()
        user = event.get("user", "unknown")

        import re
        m = re.search(
            r"(approve|hold|escalate)\s+(esc-[a-f0-9]+)(.*)?",
            text, re.IGNORECASE,
        )
        if not m:
            return None

        decision_word = m.group(1).lower()
        escalation_id = m.group(2)
        reasoning     = (m.group(3) or "").strip()

        return HumanDecision(
            escalation_id = escalation_id,
            human_id      = user,
            decision      = decision_word,
            reasoning     = reasoning,
            channel       = self.name,
            raw_response  = text,
        )

    # ── utils ──────────────────────────────────────────────────────────────────

    def _resolve_target(self, assignment: RoutingAssignment) -> str:
        """Map human_id to a Slack channel/DM target. Falls back to default."""
        hid = assignment.human_id
        # If it looks like a Slack user ID or channel, use as-is
        if hid.startswith(("U", "C", "D", "#", "@")):
            return hid
        # Otherwise use the configured default
        return self._default_channel

    def verify_signature(self, body: str, timestamp: str, signature: str) -> bool:
        """Verify a Slack webhook request signature."""
        if not self._signing_secret:
            return True   # skip verification in dev
        base   = f"v0:{timestamp}:{body}"
        digest = "v0=" + hmac.new(
            self._signing_secret.encode(),
            base.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(digest, signature)
