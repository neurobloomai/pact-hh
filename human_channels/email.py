"""
pact_hh/human_channels/email.py
──────────────────────────────────
EmailChannel — sends escalation packets as HTML emails and parses
reply-to responses back into HumanDecision.

Usage
─────
    from pact_hh.human_channels.email import EmailChannel

    channel = EmailChannel(
        smtp_host   = "smtp.gmail.com",
        smtp_port   = 587,
        username    = "alerts@example.com",
        password    = "...",
        from_addr   = "PACT Escalations <alerts@example.com>",
    )

    receipt = channel.send(packet, assignment, body)

Inbound parsing:
    decision = channel.receive({"raw_email": "...email body..."})

If smtplib fails (e.g. no network in tests), EmailChannel degrades to
dry-run mode and logs what it would have sent.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from pact_hh.escalation_packet import EscalationPacket, HumanDecision
from pact_hh.escalation_router import RoutingAssignment
from pact_hh.human_channels.base import DeliveryReceipt, HumanChannel

logger = logging.getLogger(__name__)

# Regex for extracting a decision from an email reply body
_DECISION_RE = re.compile(
    r"^\s*(approve|hold|escalate)(?:\s+(.+))?$",
    re.IGNORECASE | re.MULTILINE,
)
# Escalation ID in email headers or body
_ESC_ID_RE = re.compile(r"esc-[a-f0-9]{10}", re.IGNORECASE)


class EmailChannel(HumanChannel):
    """
    Sends escalation packets as HTML emails via SMTP.

    Parameters
    ----------
    smtp_host   : SMTP server hostname.
    smtp_port   : SMTP port (587 for STARTTLS, 465 for SSL).
    username    : SMTP authentication username.
    password    : SMTP authentication password.
    from_addr   : Sender address shown in email.
    use_tls     : Use STARTTLS (True) or plain (False). Default True.
    dry_run     : Log instead of actually sending.
    """

    name = "email"

    def __init__(
        self,
        smtp_host:  str  = "localhost",
        smtp_port:  int  = 587,
        username:   str  = "",
        password:   str  = "",
        from_addr:  str  = "pact-hh <noreply@example.com>",
        use_tls:    bool = True,
        dry_run:    bool = False,
    ) -> None:
        self._host     = smtp_host
        self._port     = smtp_port
        self._user     = username
        self._password = password
        self._from     = from_addr
        self._use_tls  = use_tls
        self._dry_run  = dry_run

    # ── HumanChannel interface ─────────────────────────────────────────────────

    def send(
        self,
        packet:     EscalationPacket,
        assignment: RoutingAssignment,
        body:       str,
    ) -> DeliveryReceipt:
        to_addr = self._resolve_address(assignment)
        subject = f"[PACT] Decision Required — {packet.intent} ({packet.escalation_id})"
        html    = self._build_html(packet, body)
        plain   = packet.plain_text()

        if self._dry_run:
            logger.info(
                "[DRY RUN] EmailChannel.send → to=%r subject=%r\n%s",
                to_addr, subject, plain,
            )
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = True,
                message_id    = f"dry-run-{packet.escalation_id}@example.com",
            )

        try:
            msg = self._compose(to_addr, subject, plain, html, packet.escalation_id)
            self._smtp_send(to_addr, msg)
            message_id = msg["Message-ID"]
            logger.info(
                "Email sent for escalation %s → %s (%s)",
                packet.escalation_id, to_addr, message_id,
            )
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = True,
                message_id    = message_id,
                metadata      = {"to": to_addr},
            )
        except Exception as exc:
            logger.error("EmailChannel failed for escalation %s: %s", packet.escalation_id, exc)
            return DeliveryReceipt(
                escalation_id = packet.escalation_id,
                channel       = self.name,
                delivered     = False,
                error         = str(exc),
            )

    def receive(self, raw: Dict[str, Any]) -> Optional[HumanDecision]:
        """
        Parse an inbound email event into a HumanDecision.

        Expected raw dict fields
        ------------------------
        from_addr      : sender email address (maps to human_id)
        subject        : email subject (may contain escalation ID)
        body           : plain-text email body
        headers        : optional dict of email headers
        escalation_id  : optional pre-extracted ID (from In-Reply-To header)
        """
        from_addr     = raw.get("from_addr", "unknown")
        body          = raw.get("body", "")
        headers       = raw.get("headers", {})
        escalation_id = raw.get("escalation_id") or self._extract_esc_id(
            raw.get("subject", "") + " " + body + " " + headers.get("In-Reply-To", "")
        )

        if not escalation_id:
            logger.debug("EmailChannel.receive: no escalation_id found in email")
            return None

        # Find the decision keyword in the first non-blank line
        decision_word, reasoning = self._parse_decision(body)
        if not decision_word:
            logger.debug(
                "EmailChannel.receive: no decision keyword in email from %s", from_addr
            )
            return None

        return HumanDecision(
            escalation_id = escalation_id,
            human_id      = from_addr,
            decision      = decision_word,
            reasoning     = reasoning,
            channel       = self.name,
            raw_response  = body[:2000],   # cap stored raw
        )

    def health(self) -> bool:
        if self._dry_run:
            return True
        try:
            with smtplib.SMTP(self._host, self._port, timeout=5) as s:
                if self._use_tls:
                    s.starttls()
                return True
        except Exception:
            return False

    # ── HTML builder ───────────────────────────────────────────────────────────

    def _build_html(self, packet: EscalationPacket, body: str) -> str:
        vote_rows = "".join(
            f"<tr>"
            f"<td style='padding:4px 8px;font-weight:bold'>{v.agent_id}</td>"
            f"<td style='padding:4px 8px;color:#2563eb'>{v.decision}</td>"
            f"<td style='padding:4px 8px'>{v.confidence:.0%}</td>"
            f"<td style='padding:4px 8px;color:#6b7280'>{v.reasoning}</td>"
            f"</tr>"
            for v in packet.agent_votes
        ) or "<tr><td colspan='4'>No agent votes recorded.</td></tr>"

        recommended_block = ""
        if packet.recommended:
            recommended_block = (
                f"<p style='margin:12px 0;padding:12px;background:#ecfdf5;"
                f"border-left:4px solid #10b981;border-radius:4px'>"
                f"<strong>Recommended action:</strong> "
                f"<code style='font-size:1.1em'>{packet.recommended.upper()}</code></p>"
            )

        return f"""<!DOCTYPE html>
<html>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:0 auto;color:#1f2937">
  <div style="background:#1e40af;color:white;padding:16px 24px;border-radius:8px 8px 0 0">
    <h2 style="margin:0">🔔 Decision Required</h2>
    <p style="margin:4px 0 0;opacity:0.85">{packet.intent}</p>
  </div>
  <div style="border:1px solid #e5e7eb;border-top:none;padding:24px;border-radius:0 0 8px 8px">
    <p><strong>Session:</strong> <code>{packet.session_id}</code></p>
    <p><strong>Trigger:</strong> <code>{packet.trigger.value}</code></p>
    <p><strong>ID:</strong> <code>{packet.escalation_id}</code></p>
    <p><strong>SLA:</strong> Respond within <strong>{packet.sla_minutes} minutes</strong></p>

    <h3 style="margin-top:20px;margin-bottom:8px">Agent votes</h3>
    <table style="width:100%;border-collapse:collapse;font-size:14px">
      <thead>
        <tr style="background:#f3f4f6">
          <th style="text-align:left;padding:6px 8px">Agent</th>
          <th style="text-align:left;padding:6px 8px">Decision</th>
          <th style="text-align:left;padding:6px 8px">Confidence</th>
          <th style="text-align:left;padding:6px 8px">Reasoning</th>
        </tr>
      </thead>
      <tbody>{vote_rows}</tbody>
    </table>

    {recommended_block}

    <div style="margin-top:24px;padding:16px;background:#f9fafb;border-radius:6px;
                font-family:monospace;font-size:14px">
      <strong>Reply to this email with one of:</strong><br><br>
      &nbsp;&nbsp;approve [optional reasoning]<br>
      &nbsp;&nbsp;hold [optional reasoning]<br>
      &nbsp;&nbsp;escalate [name or team]<br>
    </div>

    <p style="margin-top:16px;font-size:12px;color:#9ca3af">
      pact-hh · escalation {packet.escalation_id}
    </p>
  </div>
</body>
</html>"""

    # ── SMTP helpers ───────────────────────────────────────────────────────────

    def _compose(
        self,
        to_addr:       str,
        subject:       str,
        plain:         str,
        html:          str,
        escalation_id: str,
    ) -> MIMEMultipart:
        import email.utils
        msg = MIMEMultipart("alternative")
        msg["Subject"]    = subject
        msg["From"]       = self._from
        msg["To"]         = to_addr
        msg["Message-ID"] = f"<{escalation_id}@pact-hh>"
        msg["Date"]       = email.utils.formatdate(localtime=True)
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html,  "html"))
        return msg

    def _smtp_send(self, to_addr: str, msg: MIMEMultipart) -> None:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(self._host, self._port) as s:
            if self._use_tls:
                s.starttls(context=ctx)
            if self._user:
                s.login(self._user, self._password)
            s.sendmail(self._from, [to_addr], msg.as_string())

    # ── parsers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_decision(body: str):
        """
        Extract (decision_word, reasoning) from email body.
        Looks for the first line matching approve / hold / escalate.
        """
        # Strip quoted reply lines (starting with ">")
        clean = "\n".join(
            line for line in body.splitlines()
            if not line.strip().startswith(">")
        )
        m = _DECISION_RE.search(clean)
        if not m:
            return None, ""
        return m.group(1).lower(), (m.group(2) or "").strip()

    @staticmethod
    def _extract_esc_id(text: str) -> Optional[str]:
        m = _ESC_ID_RE.search(text)
        return m.group(0) if m else None

    @staticmethod
    def _resolve_address(assignment: RoutingAssignment) -> str:
        """Use human_id as email address if it contains '@', else raise."""
        hid = assignment.human_id
        if "@" in hid:
            return hid
        raise ValueError(
            f"EmailChannel: human_id {hid!r} is not an email address. "
            "Set human_id to a valid email in your RoutingRule."
        )
