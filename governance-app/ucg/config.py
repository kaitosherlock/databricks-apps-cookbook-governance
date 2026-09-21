"""Deployment configuration, read once from the environment.

Every switch here is server-side. Nothing a browser sends can change a value in
this module, which is what makes the role mapping below an authorisation input
rather than a UI hint.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping


class Role:
    """Application roles, ordered from least to most privileged."""

    VIEWER = "viewer"
    AUDITOR = "auditor"
    STEWARD = "steward"
    ACCESS_ADMIN = "access_admin"
    PLATFORM_ADMIN = "platform_admin"

    ALL = (VIEWER, AUDITOR, STEWARD, ACCESS_ADMIN, PLATFORM_ADMIN)

    LABELS = {
        VIEWER: "Người xem",
        AUDITOR: "Kiểm toán",
        STEWARD: "Data Steward",
        ACCESS_ADMIN: "Quản trị quyền truy cập",
        PLATFORM_ADMIN: "Quản trị nền tảng",
    }

    DESCRIPTIONS = {
        VIEWER: "Xem tài sản dữ liệu và quyền truy cập. Không thay đổi được gì.",
        AUDITOR: "Như Người xem, và xem thêm nhật ký kiểm toán cùng các báo cáo rà soát.",
        STEWARD: "Sửa mô tả và gán thẻ (tag) cho tài sản dữ liệu. Không cấp/thu hồi quyền.",
        ACCESS_ADMIN: "Cấp và thu hồi quyền, chuyển quyền sở hữu trong phạm vi được cấu hình.",
        PLATFORM_ADMIN: "Toàn quyền trong ứng dụng, gồm cả lưu trữ, chia sẻ dữ liệu và chính sách.",
    }


def _rank(role: str) -> int:
    try:
        return Role.ALL.index(role)
    except ValueError:
        return 0


def csv_set(value: str, lowercase: bool = False) -> frozenset[str]:
    items = (x.strip() for x in (value or "").split(","))
    return frozenset(x.lower() if lowercase else x for x in items if x)


def _parse_roles(raw: str) -> dict[str, str]:
    """Parse ``email:role,email:role``. Unknown roles are dropped, not guessed."""
    mapping: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        email, _, role = entry.rpartition(":")
        email, role = email.strip().lower(), role.strip().lower()
        if not email or role not in Role.ALL:
            continue
        # Highest role wins if an address is listed more than once.
        if email not in mapping or _rank(role) > _rank(mapping[email]):
            mapping[email] = role
    return mapping


def _flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Everything the backend needs to know about this deployment."""

    #: Optional catalog allowlist. Empty means "every catalog the executing
    #: identity can see" - for reads and writes alike.
    catalogs: frozenset[str] = frozenset()
    #: email -> role. Addresses absent from the map get :data:`default_role`.
    roles: Mapping[str, str] = field(default_factory=dict)
    default_role: str = Role.VIEWER
    #: Master switch. With writes off the app is strictly read-only, whatever
    #: role a user holds and whatever Unity Catalog would allow.
    enable_writes: bool = False
    #: Running against a developer's own CLI profile. Always read-only.
    local: bool = False
    #: Free-text environment label (DEV/UAT/PROD). Never inferred.
    environment: str = ""
    #: Warehouse used for the documented SQL-backed modules. Optional.
    warehouse_id: str = ""
    #: Hard ceilings for SQL-backed reads.
    sql_row_limit: int = 1000
    sql_timeout_seconds: int = 50
    #: Page sizes for UC list calls.
    page_size: int = 500
    max_pages: int = 200

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        get = env.get

        roles = _parse_roles(get("GOVERNANCE_ROLES", ""))
        # Backwards compatible bootstrap: the old admin allowlist maps onto the
        # access-administrator role unless GOVERNANCE_ROLES already said more.
        for email in csv_set(get("GOVERNANCE_ADMIN_EMAILS", ""), True):
            if _rank(Role.ACCESS_ADMIN) > _rank(roles.get(email, Role.VIEWER)):
                roles[email] = Role.ACCESS_ADMIN
        for email in csv_set(get("GOVERNANCE_PLATFORM_ADMIN_EMAILS", ""), True):
            roles[email] = Role.PLATFORM_ADMIN
        for email in csv_set(get("GOVERNANCE_STEWARD_EMAILS", ""), True):
            if _rank(Role.STEWARD) > _rank(roles.get(email, Role.VIEWER)):
                roles[email] = Role.STEWARD
        for email in csv_set(get("GOVERNANCE_AUDITOR_EMAILS", ""), True):
            if _rank(Role.AUDITOR) > _rank(roles.get(email, Role.VIEWER)):
                roles[email] = Role.AUDITOR

        default_role = (get("GOVERNANCE_DEFAULT_ROLE", "") or Role.VIEWER).strip().lower()
        if default_role not in Role.ALL:
            default_role = Role.VIEWER

        environment = (get("GOVERNANCE_ENVIRONMENT", "") or "").strip().upper()

        return cls(
            catalogs=csv_set(get("GOVERNANCE_CATALOGS", "")),
            roles=roles,
            default_role=default_role,
            enable_writes=_flag(get("GOVERNANCE_ENABLE_WRITES")),
            local=_flag(get("GOVERNANCE_LOCAL")),
            environment=environment,
            warehouse_id=(get("GOVERNANCE_WAREHOUSE_ID", "") or "").strip(),
            sql_row_limit=_positive_int(get("GOVERNANCE_SQL_ROW_LIMIT"), 1000, 10_000),
            sql_timeout_seconds=_positive_int(get("GOVERNANCE_SQL_TIMEOUT_SECONDS"), 50, 50),
            page_size=_positive_int(get("GOVERNANCE_PAGE_SIZE"), 500, 1000),
            max_pages=_positive_int(get("GOVERNANCE_MAX_PAGES"), 200, 5000),
        )

    def role_for(self, email: str) -> str:
        return self.roles.get((email or "").strip().lower(), self.default_role)

    def in_scope(self, catalog: str) -> bool:
        return not self.catalogs or catalog in self.catalogs

    @property
    def scope_label(self) -> str:
        if not self.catalogs:
            return "Tất cả catalog mà danh tính thực thi nhìn thấy"
        return ", ".join(sorted(self.catalogs))

    @property
    def writes_possible(self) -> bool:
        """Whether *any* write can happen in this deployment."""
        return self.enable_writes and not self.local

    def write_block_reason(self) -> str:
        if self.local:
            return "Ứng dụng đang chạy ở chế độ local (chỉ đọc)."
        if not self.enable_writes:
            return "Cấu hình GOVERNANCE_ENABLE_WRITES đang tắt."
        return ""

    def describe(self) -> list[dict[str, str]]:
        """Non-secret configuration, for the diagnostics screen."""
        return [
            {"key": "GOVERNANCE_CATALOGS", "value": ", ".join(sorted(self.catalogs)) or "(trống — không giới hạn)"},
            {"key": "GOVERNANCE_ENABLE_WRITES", "value": "true" if self.enable_writes else "false"},
            {"key": "GOVERNANCE_LOCAL", "value": "true" if self.local else "false"},
            {"key": "GOVERNANCE_ENVIRONMENT", "value": self.environment or "(chưa đặt)"},
            {"key": "GOVERNANCE_DEFAULT_ROLE", "value": self.default_role},
            {"key": "GOVERNANCE_ROLES", "value": f"{len(self.roles)} địa chỉ đã gán vai trò"},
            {"key": "GOVERNANCE_WAREHOUSE_ID", "value": self.warehouse_id or "(chưa đặt)"},
        ]


def _positive_int(raw: str | None, default: int, ceiling: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, ceiling)


def known_environments() -> Iterable[str]:
    return ("DEV", "UAT", "STAGING", "PROD")
