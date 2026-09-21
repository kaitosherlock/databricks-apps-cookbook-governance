"""The app's own operation log.

This is deliberately *not* called an audit trail. Databricks Apps does not
persist logs past the compute lifetime, so what this module produces is an
operational record written to stdout plus an in-session list for the UI. The
authoritative audit record is Databricks' own ``system.access.audit``, which the
audit module reads separately.

Every event records the actor and the execution identity as two distinct
fields, because they are two distinct identities and conflating them is how a
governance tool ends up misattributing a change.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

LOGGER_NAME = "ucg.operations"


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


LOG = configure_logging()


class Status:
    """Lifecycle of one attempted change. ``UNKNOWN`` is a first-class outcome."""

    PLANNED = "planned"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    UNKNOWN = "unknown"
    VERIFIED = "verified"
    VERIFY_FAILED = "verify_failed"

    LABELS = {
        PLANNED: "Đã lập kế hoạch",
        APPLYING: "Đang thực hiện",
        APPLIED: "Đã gửi thành công",
        FAILED: "Thất bại",
        UNKNOWN: "Chưa xác định kết quả",
        VERIFIED: "Đã xác minh trên Databricks",
        VERIFY_FAILED: "Đã gửi nhưng chưa xác minh được",
    }

    #: Outcomes where a blind retry could duplicate the change.
    NEEDS_RECONCILE = frozenset({UNKNOWN, VERIFY_FAILED})


@dataclass
class Event:
    action: str
    target: str
    target_type: str
    actor: str
    execution_identity: str
    status: str
    summary: str = ""
    reason: str = ""
    principal: str = ""
    details: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid4().hex)
    operation_id: str = ""
    correlation_id: str = ""
    time_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error_code: str = ""

    @property
    def status_label(self) -> str:
        return Status.LABELS.get(self.status, self.status)

    def row(self) -> dict:
        return {
            "Thời điểm (UTC)": self.time_utc.replace("T", " ")[:19],
            "Người thao tác": self.actor or "—",
            "Hành động": self.summary or self.action,
            "Đối tượng": self.target,
            "Kết quả": self.status_label,
            "Event ID": self.event_id[:12],
        }

    def as_dict(self) -> dict:
        return asdict(self)


class OperationLog:
    """Writes to stdout and keeps the current session's events for display."""

    def __init__(self, limit: int = 200):
        self.limit = limit
        self._events: list[Event] = []

    def record(self, event: Event) -> Event:
        # The message is machine-readable; it never contains a token, a secret,
        # or an upstream response body.
        LOG.info(json.dumps(event.as_dict(), ensure_ascii=False))
        self._events.insert(0, event)
        del self._events[self.limit:]
        return event

    def update(self, event_id: str, **changes) -> Event | None:
        for event in self._events:
            if event.event_id == event_id:
                for key, value in changes.items():
                    setattr(event, key, value)
                LOG.info(json.dumps(event.as_dict(), ensure_ascii=False))
                return event
        return None

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def rows(self) -> list[dict]:
        return [e.row() for e in self._events]

    def needing_reconcile(self) -> list[Event]:
        return [e for e in self._events if e.status in Status.NEEDS_RECONCILE]

    #: Shown wherever this log is displayed, so nobody mistakes it for the
    #: Databricks audit log.
    DISCLAIMER = (
        "Đây là nhật ký thao tác **trong phiên làm việc này** của ứng dụng, không phải "
        "nhật ký kiểm toán đầy đủ của Databricks. Databricks ghi nhận thao tác API dưới "
        "danh tính thực thi; bản ghi đầy đủ nằm ở `system.access.audit`."
    )
