"""The mutation pipeline every change goes through.

    Validate -> Authorize -> Build plan -> Preview -> Confirm
             -> Revalidate -> Execute -> Verify -> Audit

A :class:`Plan` is the artefact that survives between "Preview" and "Confirm".
It is bound to the actor, the execution identity, the target and a fingerprint
of the state it was built against. Anything that changes the substance of the
plan invalidates it, so a stale preview can never be applied - which is exactly
the failure Streamlit reruns invite.

Nothing here retries. A timeout after a change has been sent leaves the plan in
``UNKNOWN``, and the only supported next step is to re-read state.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .audit import Status
from .errors import Code, GovernanceError

#: A preview older than this is refused. Long enough for a careful read,
#: short enough that the world has probably not moved on.
PLAN_TTL = timedelta(minutes=15)


def fingerprint(payload) -> str:
    """Stable hash of the state a plan was built against."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class PreviewLine:
    """One human-readable statement about what will happen."""

    text: str
    kind: str = "info"  # info | change | warning | unknown


@dataclass
class Plan:
    """A validated, authorised, not-yet-executed change."""

    action: str
    target_key: str
    target_name: str
    target_type: str
    actor: str
    execution_identity: str
    #: What the change is, in a form the executor understands.
    payload: dict
    #: Hash of the pre-change state. Re-checked immediately before executing.
    before: str
    reason: str = ""
    summary: str = ""
    preview: list[PreviewLine] = field(default_factory=list)
    #: Idempotency key. Sent to APIs that accept one and recorded either way, so
    #: a duplicated submit is recognisable in the operation log.
    operation_id: str = field(default_factory=lambda: uuid4().hex)
    plan_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = Status.PLANNED
    #: Set once the plan has been handed to the executor, so a rerun of the
    #: page cannot submit it twice.
    consumed: bool = False
    #: True when the operation is not atomic across the objects it touches.
    multi_object: bool = False

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) - self.created_at > PLAN_TTL

    @property
    def age_seconds(self) -> int:
        return int((datetime.now(timezone.utc) - self.created_at).total_seconds())

    def binding(self, actor: str, execution_identity: str, target_key: str) -> bool:
        """Does this plan belong to the context the user is currently in?"""
        return (
            self.actor == actor
            and self.execution_identity == execution_identity
            and self.target_key == target_key
        )

    def require_fresh(self, actor: str, execution_identity: str, target_key: str):
        if self.consumed:
            raise GovernanceError(
                Code.STALE_PLAN,
                "Bản xem trước này đã được gửi đi. Tạo bản xem trước mới nếu muốn thực hiện lại.",
            )
        if self.expired:
            raise GovernanceError(
                Code.STALE_PLAN,
                "Bản xem trước đã quá hạn. Hãy tạo lại để làm việc trên trạng thái mới nhất.",
            )
        if not self.binding(actor, execution_identity, target_key):
            raise GovernanceError(
                Code.STALE_PLAN,
                "Bản xem trước thuộc về ngữ cảnh khác (người dùng hoặc đối tượng đã đổi).",
            )

    def require_unchanged(self, current_before: str):
        """The revalidate step: refuse if the world moved since the preview.

        This is a compare-and-check, not a transaction: Unity Catalog offers no
        transaction spanning read and update, so this narrows the window without
        closing it.
        """
        if current_before != self.before:
            raise GovernanceError(
                Code.STALE_PLAN,
                "Trạng thái đã thay đổi sau khi xem trước. Hãy tạo bản xem trước mới.",
            )

    def require_confirmation(self, typed: str):
        if (typed or "").strip() != self.target_name:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Tên xác nhận không khớp với tên đầy đủ của đối tượng.",
            )

    def preview_rows(self) -> list[dict]:
        return [{"Nội dung": line.text, "Loại": line.kind} for line in self.preview]

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "operation_id": self.operation_id,
            "action": self.action,
            "target": self.target_name,
            "target_type": self.target_type,
            "actor": self.actor,
            "execution_identity": self.execution_identity,
            "reason": self.reason,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
        }


@dataclass
class ObjectOutcome:
    """Result for one object inside a multi-object operation."""

    target: str
    status: str
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (Status.APPLIED, Status.VERIFIED)

    def row(self) -> dict:
        return {
            "Đối tượng": self.target,
            "Kết quả": Status.LABELS.get(self.status, self.status),
            "Chi tiết": self.message,
        }


@dataclass
class Outcome:
    """What actually happened. Never collapses partial success into success."""

    status: str
    event_id: str = ""
    operation_id: str = ""
    message: str = ""
    per_object: list[ObjectOutcome] = field(default_factory=list)
    #: Populated by the verify step: the state read back after the change.
    verified_state: dict | None = None
    error: GovernanceError | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in (Status.APPLIED, Status.VERIFIED)

    @property
    def unknown(self) -> bool:
        return self.status in Status.NEEDS_RECONCILE

    @property
    def partial(self) -> bool:
        if not self.per_object:
            return False
        good = sum(1 for o in self.per_object if o.ok)
        return 0 < good < len(self.per_object)

    def headline(self) -> str:
        if self.partial:
            good = sum(1 for o in self.per_object if o.ok)
            return f"Thành công một phần: {good}/{len(self.per_object)} đối tượng."
        return Status.LABELS.get(self.status, self.status)
