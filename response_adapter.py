"""
pact_hh/response_adapter.py
──────────────────────────────
HumanResponseAdapter — turns a raw human reply into a structured HumanDecision.

This is the parsing layer. It sits between the raw inbound event (a Slack
payload, an email body, a webhook JSON blob, a CLI string) and the typed
HumanDecision that gets injected back into the CoordinationBus.

Responsibilities
────────────────
  1. Identify which escalation the response is for (escalation_id lookup)
  2. Normalise the decision keyword (approve / hold / escalate / custom)
  3. Extract reasoning from free-form text
  4. Derive a confidence signal from linguistic certainty markers
  5. Return a fully-typed HumanDecision ready for DecisionInjector

Usage
─────
    adapter = HumanResponseAdapter(store=escalation_store)

    # From Slack
    decision = adapter.from_slack(slack_payload)

    # From email
    decision = adapter.from_email(email_dict)

    # From a raw string (CLI, webhook, custom UI)
    decision = adapter.from_text(
        text          = "approve — customer has clean 3yr history",
        escalation_id = "esc-abc12345",
        human_id      = "ops-lead",
        channel       = "webhook",
    )
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from pact_hh.escalation_packet import EscalationPacket, HumanDecision
from pact_hh.escalation_store import EscalationStore

logger = logging.getLogger(__name__)


# ── Known decision keywords and their canonical form ──────────────────────────

_DECISION_ALIASES: Dict[str, str] = {
    # approve family
    "approve":    "approve",
    "approved":   "approve",
    "yes":        "approve",
    "accept":     "approve",
    "confirm":    "approve",
    "accepted":   "approve",
    "go":         "approve",
    "proceed":    "approve",
    "ok":         "approve",
    "okay":       "approve",

    # hold / pause family
    "hold":       "hold",
    "wait":       "hold",
    "pause":      "hold",
    "defer":      "hold",
    "pending":    "hold",
    "later":      "hold",
    "no":         "hold",
    "reject":     "hold",
    "deny":       "hold",
    "block":      "hold",

    # escalate family
    "escalate":   "escalate",
    "elevate":    "escalate",
    "forward":    "escalate",
    "transfer":   "escalate",
    "pass":       "escalate",
}

# Confidence boosters / reducers from linguistic signals
_HIGH_CONFIDENCE = re.compile(
    r"\b(definitely|clearly|absolutely|certainly|100%|no doubt|obviously)\b",
    re.IGNORECASE,
)
_LOW_CONFIDENCE = re.compile(
    r"\b(maybe|perhaps|not sure|unsure|uncertain|i think|probably|could be|might)\b",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _DECISION_ALIASES) + r")\b",
    re.IGNORECASE,
)
_ESC_ID_RE = re.compile(r"esc-[a-f0-9]{8,12}", re.IGNORECASE)


class HumanResponseAdapter:
    """
    Parses human replies from any channel into structured HumanDecision objects.

    Parameters
    ----------
    store            : EscalationStore used to validate open escalations.
    default_confidence : Confidence level assigned to human decisions
                         when no linguistic signal is found. Default 0.95.
    allow_custom     : If True, unknown decision keywords are kept as-is
                       rather than returning None. Default False.
    """

    def __init__(
        self,
        store:              Optional[EscalationStore] = None,
        default_confidence: float                     = 0.95,
        allow_custom:       bool                      = False,
    ) -> None:
        self._store      = store
        self._default_conf = default_confidence
        self._allow_custom = allow_custom

    # ── Primary entry points ───────────────────────────────────────────────────

    def from_text(
        self,
        text:          str,
        escalation_id: str,
        human_id:      str,
        channel:       str = "unknown",
    ) -> Optional[HumanDecision]:
        """
        Parse a plain-text response string into a HumanDecision.

        text may be:
          - "approve"
          - "approve — customer has clean history"
          - "hold not sure about the amount"
          - "escalate to legal-team, policy concern"
        """
        decision, reasoning = self._extract_decision_and_reasoning(text)
        if decision is None:
            logger.debug(
                "from_text: no recognisable decision in %r (escalation=%s)",
                text, escalation_id,
            )
            return None

        confidence = self._infer_confidence(text)

        self._log_and_validate(escalation_id, human_id, decision)

        return HumanDecision(
            escalation_id = escalation_id,
            human_id      = human_id,
            decision      = decision,
            reasoning     = reasoning,
            confidence    = confidence,
            channel       = channel,
            raw_response  = text,
        )

    def from_dict(self, data: Dict) -> Optional[HumanDecision]:
        """
        Build a HumanDecision from a pre-parsed dict (e.g. from webhook payload).

        Expected keys: escalation_id, human_id, decision, reasoning?, confidence?
        """
        escalation_id = data.get("escalation_id", "")
        human_id      = data.get("human_id", "unknown")
        raw_decision  = data.get("decision", "").lower().strip()

        if not escalation_id or not raw_decision:
            return None

        canonical = self._canonicalise(raw_decision)
        if canonical is None:
            return None

        return HumanDecision(
            escalation_id = escalation_id,
            human_id      = human_id,
            decision      = canonical,
            reasoning     = data.get("reasoning", ""),
            confidence    = float(data.get("confidence", self._default_conf)),
            channel       = data.get("channel", "unknown"),
            raw_response  = str(data),
        )

    def from_slack(self, payload: Dict) -> Optional[HumanDecision]:
        """Delegate to SlackChannel.receive() and normalise the result."""
        from pact_hh.human_channels.slack import SlackChannel
        channel = SlackChannel(dry_run=True)   # parsing only — no API calls
        decision = channel.receive(payload)
        if decision is None:
            return None
        return self._normalise(decision)

    def from_email(self, email_dict: Dict) -> Optional[HumanDecision]:
        """Delegate to EmailChannel.receive() and normalise the result."""
        from pact_hh.human_channels.email import EmailChannel
        channel = EmailChannel(dry_run=True)
        decision = channel.receive(email_dict)
        if decision is None:
            return None
        return self._normalise(decision)

    def from_webhook(self, payload: Dict) -> Optional[HumanDecision]:
        """Delegate to WebhookChannel.receive() and normalise the result."""
        from pact_hh.human_channels.webhook import WebhookChannel
        channel = WebhookChannel(dry_run=True)
        decision = channel.receive(payload)
        if decision is None:
            return None
        return self._normalise(decision)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _normalise(self, decision: HumanDecision) -> Optional[HumanDecision]:
        """Canonicalise decision keyword and re-derive confidence."""
        canonical = self._canonicalise(decision.decision)
        if canonical is None:
            return None
        confidence = self._infer_confidence(decision.raw_response) if decision.raw_response else decision.confidence
        decision.decision   = canonical
        decision.confidence = confidence
        return decision

    def _extract_decision_and_reasoning(self, text: str) -> Tuple[Optional[str], str]:
        """
        Find the first decision keyword in text; everything after is reasoning.
        Returns (canonical_decision, reasoning_string).
        """
        m = _DECISION_RE.search(text)
        if not m:
            return None, ""

        raw_word  = m.group(1)
        canonical = self._canonicalise(raw_word)
        if canonical is None:
            return None, ""

        # Everything after the matched keyword is the reasoning
        reasoning = text[m.end():].strip(" \t—-–:,")
        return canonical, reasoning

    def _canonicalise(self, word: str) -> Optional[str]:
        """Map an arbitrary word to its canonical decision. None if unrecognised."""
        lowered = word.lower().strip()
        canonical = _DECISION_ALIASES.get(lowered)
        if canonical:
            return canonical
        if self._allow_custom:
            return lowered
        return None

    def _infer_confidence(self, text: str) -> float:
        """Adjust default confidence based on linguistic certainty signals."""
        conf = self._default_conf
        if _HIGH_CONFIDENCE.search(text):
            conf = min(1.0, conf + 0.03)
        if _LOW_CONFIDENCE.search(text):
            conf = max(0.5, conf - 0.15)
        return round(conf, 3)

    def _log_and_validate(
        self,
        escalation_id: str,
        human_id:      str,
        decision:      str,
    ) -> None:
        logger.info(
            "Response parsed: escalation=%s human=%s decision=%r",
            escalation_id, human_id, decision,
        )
        if self._store:
            record = self._store.get(escalation_id)
            if record is None:
                logger.warning(
                    "HumanResponseAdapter: escalation %s not found in store",
                    escalation_id,
                )
            elif not record.is_open:
                logger.warning(
                    "HumanResponseAdapter: escalation %s is already %s",
                    escalation_id, record.status.value,
                )

    # ── Batch / fan-out ───────────────────────────────────────────────────────

    def parse_many(self, messages: List[Dict]) -> List[HumanDecision]:
        """
        Parse a list of raw message dicts.
        Each dict must have a 'channel' key ('slack', 'email', 'webhook', 'text').
        Returns only successfully parsed decisions.
        """
        results = []
        for msg in messages:
            ch = msg.get("channel", "text")
            try:
                if ch == "slack":
                    d = self.from_slack(msg)
                elif ch == "email":
                    d = self.from_email(msg)
                elif ch == "webhook":
                    d = self.from_webhook(msg)
                else:
                    d = self.from_dict(msg)
                if d:
                    results.append(d)
            except Exception as exc:
                logger.error("parse_many: failed to parse message %r: %s", msg, exc)
        return results
