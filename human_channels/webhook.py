"""
pact_hh/human_channels/webhook.py
────────────────────────────────────
WebhookChannel — sends escalation packets as JSON HTTP POST requests
and accepts decision callbacks on a configurable endpoint.

Useful for:
  - Custom internal tooling that already has a notification pipeline
  - PagerDuty / Opsgenie / Jira-style webhook integrations
  - Testing and CI — point at a mock server and inspect payloads

Usage
─────
    from pact_hh.human_channels.webhook import WebhookChannel

    channel = WebhookChannel(
        endpoint = "https://myapp.internal/escalation-hook",
        secret   = "shared-hmac-secret",   # optional HMAC signing
        timeout  = 10,
    )

    receipt = channel.send(packet, assignment, body)

Inbound callback (from your server calling pact-hh back):
    decision = channel.receive({
        "escalation_id": "esc-abc123",
        "human_id":      "ops-lead",
        "decision":      "approve",
        "reasoning":     "Looks good.",
    })

No required dependencies beyond the stdlib urllib. requests/httpx will be
used if available (better connection pooling and TLS), otherwise urllib.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from pact_hh.escalation_packet import EscalationPacket, HumanDecision
from pact_hh.escalation_router import RoutingAssignment
from pact_hh.human_channels.base import DeliveryReceipt, HumanChannel

logger = logging.getLogger(__name__)

# Try requests first, httpx second, fall back to urllib
try:
    import requests as _requests_lib
    _HTTP = "requests"
except ImportError:
    try:
        import httpx as _requests_lib   # type: ignore[no-redef]
        _HTTP = "httpx"
    except ImportError:
        _requests_lib = None
        _HTTP = "urllib"


class WebhookChannel(HumanChannel):
    """
    Sends escalation packets as JSON webhook POST requests.

    Parameters
    ----------
    endpoint    : URL to POST the escalation payload to.
    secret      : HMAC-SHA256 signing secret. If set, adds
                  X-PACT-Signature header for verification.
    timeout     : HTTP request timeout in seconds. Default 10.
    headers     : Extra HTTP headers to include in every request.
    dry_run     : Log instead of actually sending.
    """

    name = "webhook"

    def __init__(
        self,
        endpoint: str              = "",
        secret:   str              = "",
        timeout:  int              = 10,
        headers:  Dict[str, str]   = None,
        dry_run:  bool             = False,
    ) -> None:
        self._endpoint = endpoint
        self._secret   = secret
        self._timeout  = timeout
        self._headers  = headers or {}
        self._dry_run  = dry_run

    # ── HumanChannel interface ─────────────────────────────────────────────────

    def send(
        self,
        packet:     EscalationPacket,
        assignment: RoutingAssignment,
        body:       str,
    ) -> DeliveryReceipt:
        """POST the escalation packet as JSON to the configured endpoint."""
        payload = self._build_payload(packet, assignment, body)

        if self._dry_run:
            logger.info(
                "[DRY RUN] WebhookChannel.send → %r\n%s",
                self._endpoint, json.dumps(payload, indent=2),
            )
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = True,
                message_id    = f"dry-run-{packet.escalation_id}",
            )

        if not self._endpoint:
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = False,
                error         = "WebhookChannel: no endpoint configured",
            )

        try:
            body_bytes = json.dumps(payload).encode()
            headers    = self._build_headers(body_bytes)
            status, response_text = self._http_post(
                self._endpoint, body_bytes, headers
            )

            success = 200 <= status < 300
            if success:
                logger.info(
                    "Webhook delivered escalation %s → %s (HTTP %d)",
                    packet.escalation_id, self._endpoint, status,
                )
            else:
                logger.warning(
                    "Webhook returned HTTP %d for escalation %s",
                    status, packet.escalation_id,
                )

            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = success,
                error         = None if success else f"HTTP {status}",
                metadata      = {"http_status": status, "endpoint": self._endpoint},
            )

        except Exception as exc:
            logger.error(
                "WebhookChannel failed for escalation %s: %s",
                packet.escalation_id, exc,
            )
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = False,
                error         = str(exc),
            )

    def receive(self, raw: Dict[str, Any]) -> Optional[HumanDecision]:
        """
        Parse a webhook callback payload into a HumanDecision.

        Expected JSON body
        ------------------
        {
            "escalation_id": "esc-abc123",
            "human_id":      "ops-lead",
            "decision":      "approve",           # required
            "reasoning":     "Looks good.",       # optional
            "confidence":    0.95,                # optional, default 0.95
        }
        """
        escalation_id = raw.get("escalation_id")
        decision_word = raw.get("decision", "").lower().strip()
        human_id      = raw.get("human_id", "unknown")

        if not escalation_id or not decision_word:
            logger.debug(
                "WebhookChannel.receive: missing escalation_id or decision in %r", raw
            )
            return None

        return HumanDecision(
            escalation_id = escalation_id,
            human_id      = human_id,
            decision      = decision_word,
            reasoning     = raw.get("reasoning", ""),
            confidence    = float(raw.get("confidence", 0.95)),
            channel       = self.name,
            raw_response  = json.dumps(raw),
        )

    def health(self) -> bool:
        if self._dry_run or not self._endpoint:
            return bool(self._dry_run)
        try:
            status, _ = self._http_post(
                self._endpoint,
                json.dumps({"pact_hh": "health_check"}).encode(),
                self._build_headers(b""),
            )
            return status < 500
        except Exception:
            return False

    # ── payload builder ───────────────────────────────────────────────────────

    def _build_payload(
        self,
        packet:     EscalationPacket,
        assignment: RoutingAssignment,
        body:       str,
    ) -> Dict:
        """Construct the JSON payload sent to the webhook."""
        return {
            "pact_hh_version":  "0.1.0",
            "event":            "escalation.created",
            "timestamp":        int(time.time()),
            "escalation":       packet.to_dict(),
            "assignment": {
                "human_id":     assignment.human_id,
                "channel":      assignment.channel,
                "rule_matched": assignment.rule_matched,
            },
            "rendered_body":    body,
        }

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _build_headers(self, body_bytes: bytes) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            **self._headers,
        }
        if self._secret:
            sig = hmac.new(
                self._secret.encode(),
                body_bytes,
                hashlib.sha256,
            ).hexdigest()
            headers["X-PACT-Signature"] = f"sha256={sig}"
        return headers

    def _http_post(
        self,
        url:     str,
        body:    bytes,
        headers: Dict[str, str],
    ):
        """POST *body* to *url*. Returns (status_code, response_text)."""
        if _HTTP in ("requests", "httpx"):
            resp = _requests_lib.post(
                url,
                data    = body,
                headers = headers,
                timeout = self._timeout,
            )
            return resp.status_code, resp.text

        # urllib fallback
        req  = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status, resp.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")

    @staticmethod
    def verify_signature(body_bytes: bytes, signature: str, secret: str) -> bool:
        """
        Verify an inbound X-PACT-Signature header.
        Use this in your webhook receiver to confirm the payload is genuine.
        """
        expected = "sha256=" + hmac.new(
            secret.encode(),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
