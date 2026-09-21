"""Application-level authorisation.

This is a *second* gate, not a replacement for Unity Catalog. Unity Catalog
still decides whether the executing identity may do anything at all; this module
decides whether the signed-in person is allowed to ask for it through this app.

Every mutation path calls :meth:`Authorizer.require` on the server side. The UI
also calls :meth:`Authorizer.allows` to grey out buttons, but that is a courtesy
- hiding a button is not access control, and a viewer who calls the service
directly still gets refused here.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Role, Settings
from .errors import Code, GovernanceError
from .naming import CATALOG_SCOPED, Target


class Action:
    """Every distinct thing this app can be asked to do."""

    # reads
    READ_METADATA = "read_metadata"
    READ_GRANTS = "read_grants"
    READ_TAGS = "read_tags"
    READ_POLICIES = "read_policies"
    READ_STORAGE = "read_storage"
    READ_SHARING = "read_sharing"
    READ_LINEAGE = "read_lineage"
    READ_QUALITY = "read_quality"
    READ_AUDIT = "read_audit"
    READ_REQUESTS = "read_requests"

    # writes
    GRANT = "grant"
    REVOKE = "revoke"
    TRANSFER_OWNERSHIP = "transfer_ownership"
    EDIT_METADATA = "edit_metadata"
    ASSIGN_TAG = "assign_tag"
    MANAGE_TAG_POLICY = "manage_tag_policy"
    MANAGE_POLICY = "manage_policy"
    MANAGE_STORAGE = "manage_storage"
    MANAGE_BINDINGS = "manage_bindings"
    MANAGE_SHARING = "manage_sharing"
    MANAGE_QUALITY = "manage_quality"
    SUBMIT_REQUEST = "submit_request"
    MANAGE_REQUEST_ROUTING = "manage_request_routing"

    LABELS = {
        READ_METADATA: "Xem metadata",
        READ_GRANTS: "Xem quyền truy cập",
        READ_TAGS: "Xem thẻ (tag)",
        READ_POLICIES: "Xem chính sách",
        READ_STORAGE: "Xem lưu trữ và credential",
        READ_SHARING: "Xem chia sẻ dữ liệu",
        READ_LINEAGE: "Xem lineage",
        READ_QUALITY: "Xem chất lượng dữ liệu",
        READ_AUDIT: "Xem nhật ký kiểm toán",
        READ_REQUESTS: "Xem cấu hình yêu cầu truy cập",
        GRANT: "Cấp quyền",
        REVOKE: "Thu hồi quyền",
        TRANSFER_OWNERSHIP: "Chuyển quyền sở hữu",
        EDIT_METADATA: "Sửa mô tả",
        ASSIGN_TAG: "Gán / gỡ thẻ",
        MANAGE_TAG_POLICY: "Quản lý governed tag",
        MANAGE_POLICY: "Quản lý chính sách ABAC",
        MANAGE_STORAGE: "Quản lý lưu trữ và credential",
        MANAGE_BINDINGS: "Quản lý ràng buộc workspace",
        MANAGE_SHARING: "Quản lý chia sẻ dữ liệu",
        MANAGE_QUALITY: "Quản lý giám sát chất lượng",
        SUBMIT_REQUEST: "Gửi yêu cầu truy cập",
        MANAGE_REQUEST_ROUTING: "Cấu hình nơi nhận yêu cầu truy cập",
    }


#: Actions that change something at Databricks.
MUTATIONS = frozenset({
    Action.GRANT, Action.REVOKE, Action.TRANSFER_OWNERSHIP, Action.EDIT_METADATA,
    Action.ASSIGN_TAG, Action.MANAGE_TAG_POLICY, Action.MANAGE_POLICY,
    Action.MANAGE_STORAGE, Action.MANAGE_BINDINGS, Action.MANAGE_SHARING,
    Action.MANAGE_QUALITY, Action.SUBMIT_REQUEST, Action.MANAGE_REQUEST_ROUTING,
})

_READS = frozenset({
    Action.READ_METADATA, Action.READ_GRANTS, Action.READ_TAGS, Action.READ_POLICIES,
    Action.READ_STORAGE, Action.READ_SHARING, Action.READ_LINEAGE, Action.READ_QUALITY,
    Action.READ_REQUESTS,
})

#: Role -> actions. Higher roles are unions built explicitly, so adding an
#: action never widens a lower role by accident.
_VIEWER = _READS | {Action.SUBMIT_REQUEST}
_AUDITOR = _VIEWER | {Action.READ_AUDIT}
_STEWARD = _AUDITOR | {Action.EDIT_METADATA, Action.ASSIGN_TAG}
_ACCESS_ADMIN = _STEWARD | {
    Action.GRANT, Action.REVOKE, Action.TRANSFER_OWNERSHIP,
    Action.MANAGE_REQUEST_ROUTING,
}
_PLATFORM_ADMIN = _ACCESS_ADMIN | {
    Action.MANAGE_TAG_POLICY, Action.MANAGE_POLICY, Action.MANAGE_STORAGE,
    Action.MANAGE_BINDINGS, Action.MANAGE_SHARING, Action.MANAGE_QUALITY,
}

ROLE_ACTIONS = {
    Role.VIEWER: _VIEWER,
    Role.AUDITOR: _AUDITOR,
    Role.STEWARD: _STEWARD,
    Role.ACCESS_ADMIN: _ACCESS_ADMIN,
    Role.PLATFORM_ADMIN: _PLATFORM_ADMIN,
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    code: str = ""

    def raise_if_denied(self):
        if not self.allowed:
            raise GovernanceError(self.code or Code.FORBIDDEN_ROLE, self.reason)


ALLOWED = Decision(True)


class Authorizer:
    """Decides whether one actor may perform one action on one target."""

    def __init__(self, settings: Settings, actor_email: str, role: str | None = None):
        self.settings = settings
        self.actor_email = (actor_email or "").strip().lower()
        self.role = role or settings.role_for(self.actor_email)

    # -- introspection used by the UI ------------------------------------
    @property
    def role_label(self) -> str:
        return Role.LABELS.get(self.role, self.role)

    def actions(self) -> frozenset[str]:
        return ROLE_ACTIONS.get(self.role, frozenset())

    def allows(self, action: str, target: Target | None = None) -> bool:
        return self.check(action, target).allowed

    def allowed_actions(self, target: Target | None = None) -> dict[str, dict]:
        """``allowed_actions`` for the UI: what is offered, and why not.

        This is advisory. Mutations re-check on the server before executing.
        """
        out = {}
        for action in Action.LABELS:
            decision = self.check(action, target)
            out[action] = {
                "label": Action.LABELS[action],
                "allowed": decision.allowed,
                "reason": decision.reason,
                "code": decision.code,
            }
        return out

    # -- the actual gate --------------------------------------------------
    def check(self, action: str, target: Target | None = None) -> Decision:
        mutating = action in MUTATIONS

        if mutating and not self.settings.writes_possible:
            return Decision(False, self.settings.write_block_reason(), Code.WRITES_DISABLED)

        # An unidentified caller gets nothing that changes state. Reads still
        # work locally, where there is no proxy to supply an identity.
        if mutating and not self.actor_email:
            return Decision(
                False,
                "Không nhận được danh tính người dùng từ proxy Databricks Apps.",
                Code.NO_IDENTITY,
            )

        if action not in self.actions():
            return Decision(
                False,
                f"Vai trò “{self.role_label}” không được phép: {Action.LABELS.get(action, action)}.",
                Code.FORBIDDEN_ROLE,
            )

        if target is not None and target.kind in CATALOG_SCOPED:
            if not self.settings.in_scope(target.catalog):
                return Decision(
                    False,
                    f"Catalog “{target.catalog}” nằm ngoài phạm vi ứng dụng được phép quản lý.",
                    Code.FORBIDDEN_SCOPE,
                )

        return ALLOWED

    def require(self, action: str, target: Target | None = None):
        """Server-side enforcement. Every mutation path goes through here."""
        self.check(action, target).raise_if_denied()

    def require_scope(self, catalog: str):
        if catalog and not self.settings.in_scope(catalog):
            raise GovernanceError(
                Code.FORBIDDEN_SCOPE,
                f"Catalog “{catalog}” nằm ngoài phạm vi ứng dụng được phép quản lý.",
            )

    # -- separation of duties --------------------------------------------
    def require_not_self(self, principal: str, what: str = "phê duyệt"):
        """Block self-approval when the actor is the beneficiary."""
        if principal and self.actor_email and principal.strip().lower() == self.actor_email:
            raise GovernanceError(
                Code.SELF_APPROVAL,
                f"Bạn không thể tự {what} yêu cầu dành cho chính mình.",
            )
