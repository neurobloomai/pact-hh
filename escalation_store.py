"""
pact_hh/escalation_store.py
─────────────────────────────
EscalationStore — tracks every open escalation, enforces SLAs,
fires reminders, and marks timeouts.

An escalation that doesn't return a decision is a failure of the protocol.
This module makes sure none go silent.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from pact_hh.escalation_packet import EscalationPacket, EscalationStatus, HumanDecision

logger = logging.getLogger(__name__)


@dataclass
class EscalationRecord:
    """Live state of one escalation from open to closed."""

    packet:           EscalationPacket
    status:           EscalationStatus        = EscalationStatus.PENDING
    assigned_to:      Optional[str]           = None   # human_id
    channel:          Optional[str]           = None
    decision:         Optional[HumanDecision] = None
    reminder_count:   int                     = 0
    opened_at:        datetime                = field(default_factory=datetime.utcnow)
    closed_at:        Optional[datetime]      = None
    notes:            List[str]               = field(default_factory=list)

    @property
    def escalation_id(self) -> str:
        return self.packet.escalation_id

    @property
    def deadline(self) -> datetime:
        return self.opened_at + timedelta(minutes=self.packet.sla_minutes)

    @property
    def is_open(self) -> bool:
        return self.status == EscalationStatus.PENDING

    @property
    def age_minutes(self) -> float:
        return (datetime.utcnow() - self.opened_at).total_seconds() / 60

    @property
    def minutes_remaining(self) -> float:
        return max(0.0, (self.deadline - datetime.utcnow()).total_seconds() / 60)

    @property
    def is_overdue(self) -> bool:
        return self.is_open and datetime.utcnow() > self.deadline

    def close(self, status: EscalationStatus, decision: Optional[HumanDecision] = None) -> None:
        self.status     = status
        self.decision   = decision
        self.closed_at  = datetime.utcnow()

    def summary(self) -> Dict:
        return {
            "escalation_id":   self.escalation_id,
            "status":          self.status.value,
            "intent":          self.packet.intent,
            "assigned_to":     self.assigned_to,
            "channel":         self.channel,
            "age_minutes":     round(self.age_minutes, 1),
            "minutes_remaining": round(self.minutes_remaining, 1),
            "is_overdue":      self.is_overdue,
            "reminder_count":  self.reminder_count,
        }


# Type alias: called when SLA is breached
TimeoutHandler = Callable[[EscalationRecord], None]
# Called when reminder should be sent
ReminderHandler = Callable[[EscalationRecord], None]


class EscalationStore:
    """
    Registry of all open and closed escalations.

    Parameters
    ----------
    reminder_at_minutes : list[int]
        Minutes-remaining thresholds at which to fire a reminder.
        Default: [15, 5] — remind when 15 min and 5 min remain.
    on_timeout : callable, optional
        Called when an escalation's SLA expires with no response.
    on_reminder : callable, optional
        Called at each reminder threshold.
    max_closed_history : int
        How many closed records to retain for analytics. Default 500.
    """

    def __init__(
        self,
        reminder_at_minutes: List[int]                  = None,
        on_timeout:          Optional[TimeoutHandler]   = None,
        on_reminder:         Optional[ReminderHandler]  = None,
        max_closed_history:  int                        = 500,
    ) -> None:
        self._open:   Dict[str, EscalationRecord] = {}
        self._closed: List[EscalationRecord]      = []
        self._reminder_thresholds = sorted(reminder_at_minutes or [15, 5], reverse=True)
        self._on_timeout  = on_timeout
        self._on_reminder = on_reminder
        self._max_closed  = max_closed_history

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def open(
        self,
        packet:      EscalationPacket,
        assigned_to: Optional[str] = None,
        channel:     Optional[str] = None,
    ) -> EscalationRecord:
        """Register a new escalation as PENDING."""
        record = EscalationRecord(
            packet      = packet,
            assigned_to = assigned_to,
            channel     = channel,
        )
        self._open[packet.escalation_id] = record
        logger.info(
            "Escalation opened: %s (intent=%r, sla=%d min, assigned=%s)",
            packet.escalation_id, packet.intent, packet.sla_minutes, assigned_to,
        )
        return record

    def resolve(self, escalation_id: str, decision: HumanDecision) -> EscalationRecord:
        """
        Mark an escalation RESPONDED with the human's decision.
        Moves it from open → closed history.
        """
        record = self._require_open(escalation_id)
        record.close(EscalationStatus.RESPONDED, decision)
        self._archive(record)
        logger.info(
            "Escalation %s resolved by %s: %r",
            escalation_id, decision.human_id, decision.decision,
        )
        return record

    def escalate_further(
        self,
        escalation_id: str,
        new_assignee:  str,
        note:          str = "",
    ) -> EscalationRecord:
        """Re-assign to a different human (e.g. manager escalation)."""
        record = self._require_open(escalation_id)
        record.notes.append(f"Escalated to {new_assignee}: {note}")
        record.assigned_to  = new_assignee
        record.reminder_count = 0  # reset SLA clock
        record.packet.sla_minutes = max(record.packet.sla_minutes, 15)
        record.status = EscalationStatus.ESCALATED_FURTHER
        # Re-open as PENDING for new assignee
        record.status = EscalationStatus.PENDING
        logger.info("Escalation %s re-assigned to %s", escalation_id, new_assignee)
        return record

    def cancel(self, escalation_id: str, reason: str = "") -> bool:
        """Cancel an open escalation (e.g. agents resolved it meanwhile)."""
        if escalation_id not in self._open:
            return False
        record = self._open.pop(escalation_id)
        record.notes.append(f"Cancelled: {reason}")
        record.close(EscalationStatus.CANCELLED)
        self._archive(record)
        logger.info("Escalation %s cancelled: %s", escalation_id, reason)
        return True

    # ── SLA enforcement ───────────────────────────────────────────────────────

    def tick(self) -> Dict[str, int]:
        """
        Check all open escalations for SLA breaches and reminder thresholds.
        Call this on a regular schedule (e.g. every minute).

        Returns counts of {timeouts, reminders}.
        """
        timeouts  = 0
        reminders = 0

        for record in list(self._open.values()):
            if record.is_overdue:
                self._handle_timeout(record)
                timeouts += 1
                continue

            # Reminders — fire once per threshold
            mins_left = record.minutes_remaining
            for threshold in self._reminder_thresholds:
                if mins_left <= threshold and record.reminder_count < \
                        self._reminder_thresholds.index(threshold) + 1:
                    self._handle_reminder(record)
                    record.reminder_count += 1
                    reminders += 1
                    break

        return {"timeouts": timeouts, "reminders": reminders}

    def _handle_timeout(self, record: EscalationRecord) -> None:
        record.close(EscalationStatus.TIMED_OUT)
        self._archive(record)
        logger.warning(
            "Escalation %s TIMED OUT after %.0f min (intent=%r, assigned=%s)",
            record.escalation_id, record.age_minutes,
            record.packet.intent, record.assigned_to,
        )
        if self._on_timeout:
            try:
                self._on_timeout(record)
            except Exception as exc:
                logger.error("on_timeout handler failed: %s", exc)

    def _handle_reminder(self, record: EscalationRecord) -> None:
        logger.info(
            "Reminder #%d for escalation %s (%.0f min remaining)",
            record.reminder_count + 1, record.escalation_id, record.minutes_remaining,
        )
        if self._on_reminder:
            try:
                self._on_reminder(record)
            except Exception as exc:
                logger.error("on_reminder handler failed: %s", exc)

    # ── queries ───────────────────────────────────────────────────────────────

    def get(self, escalation_id: str) -> Optional[EscalationRecord]:
        return self._open.get(escalation_id) or next(
            (r for r in self._closed if r.escalation_id == escalation_id), None
        )

    def open_for(self, human_id: str) -> List[EscalationRecord]:
        return [r for r in self._open.values() if r.assigned_to == human_id]

    def all_open(self) -> List[EscalationRecord]:
        return list(self._open.values())

    def overdue(self) -> List[EscalationRecord]:
        return [r for r in self._open.values() if r.is_overdue]

    # ── internals ────────────────────────────────────────────────────────────

    def _require_open(self, escalation_id: str) -> EscalationRecord:
        if escalation_id not in self._open:
            raise KeyError(f"No open escalation with id {escalation_id!r}")
        return self._open.pop(escalation_id)

    def _archive(self, record: EscalationRecord) -> None:
        self._closed.append(record)
        if len(self._closed) > self._max_closed:
            self._closed = self._closed[-self._max_closed:]

    # ── metrics ───────────────────────────────────────────────────────────────

    def metrics(self) -> Dict:
        closed = self._closed
        responded = [r for r in closed if r.status == EscalationStatus.RESPONDED]
        avg_response = (
            sum(
                (r.closed_at - r.opened_at).total_seconds() / 60
                for r in responded if r.closed_at
            ) / len(responded)
            if responded else 0.0
        )
        return {
            "open_count":           len(self._open),
            "overdue_count":        len(self.overdue()),
            "closed_count":         len(closed),
            "responded_count":      len(responded),
            "timed_out_count":      sum(1 for r in closed if r.status == EscalationStatus.TIMED_OUT),
            "avg_response_minutes": round(avg_response, 1),
        }

    def __len__(self) -> int:
        return len(self._open)

    def __repr__(self) -> str:
        return f"EscalationStore(open={len(self._open)}, overdue={len(self.overdue())})"
