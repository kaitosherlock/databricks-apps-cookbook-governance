"""Small, SDK-backed governance service. No SQL execution or data-plane access."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import PermissionsChange, Privilege

LOG = logging.getLogger("governance.audit")
# Deliberately small set for this starter; Databricks remains the authority.
PRIVILEGES = {
    "catalog": ("USE_CATALOG", "USE_SCHEMA", "BROWSE", "SELECT", "MODIFY", "EXECUTE"),
    "schema": ("USE_SCHEMA", "CREATE_TABLE", "CREATE_FUNCTION", "SELECT", "MODIFY", "EXECUTE"),
    "table": ("SELECT", "MODIFY"),
    "function": ("EXECUTE",),
}


class GovernanceError(ValueError):
    pass


def csv_set(value: str, lowercase: bool = False) -> frozenset[str]:
    values = [x.strip() for x in value.split(",") if x.strip()]
    return frozenset(x.lower() if lowercase else x for x in values)


@dataclass(frozen=True)
class Settings:
    catalogs: frozenset[str]
    admins: frozenset[str]
    writes: bool = False
    local: bool = False
    demo: bool = False

    @classmethod
    def from_env(cls):
        return cls(
            csv_set(os.getenv("GOVERNANCE_CATALOGS", "")),
            csv_set(os.getenv("GOVERNANCE_ADMIN_EMAILS", ""), True),
            os.getenv("GOVERNANCE_ENABLE_WRITES", "false").lower() == "true",
            os.getenv("GOVERNANCE_LOCAL", "false").lower() == "true",
            os.getenv("GOVERNANCE_DEMO", "false").lower() == "true",
        )


@dataclass(frozen=True)
class Target:
    kind: str
    catalog: str
    schema: str = ""
    name: str = ""

    def __post_init__(self):
        if self.kind not in PRIVILEGES or not self.catalog:
            raise GovernanceError("Loại đối tượng hoặc catalog không hợp lệ.")
        if self.kind != "catalog" and not self.schema:
            raise GovernanceError("Thiếu schema.")
        if self.kind in ("table", "function") and not self.name:
            raise GovernanceError("Thiếu tên đối tượng.")
        if self.kind == "catalog" and (self.schema or self.name):
            raise GovernanceError("Catalog không có đối tượng con trong target.")
        if self.kind == "schema" and self.name:
            raise GovernanceError("Schema target không có tên đối tượng.")
        # UC full names use dot-separated components, not SQL quoted identifiers.
        if any("." in x or "\x00" in x for x in (self.catalog, self.schema, self.name)):
            raise GovernanceError("Tên thành phần không được chứa dấu chấm hoặc NUL.")

    @property
    def full_name(self):
        return ".".join(x for x in (self.catalog, self.schema, self.name) if x)


@dataclass(frozen=True)
class Change:
    target: Target
    principal: str
    action: str
    privileges: tuple[str, ...]
    reason: str
    before: str

    def preview(self):
        return {
            "object_type": self.target.kind,
            "object": self.target.full_name,
            "principal": self.principal,
            "action": self.action,
            "privileges": list(self.privileges),
            "reason": self.reason,
        }


def actor_from_headers(headers: Mapping[str, str]) -> str:
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get("x-forwarded-email", "").strip().lower()


def make_client(settings: Settings):
    if settings.local:
        return WorkspaceClient()  # local Databricks CLI OAuth profile; writes blocked
    return WorkspaceClient(auth_type="oauth-m2m")  # app identity, never implicit PAT/OBO


def as_dict(obj):
    return obj.as_dict() if hasattr(obj, "as_dict") else obj


def enum_value(value):
    return value.value if hasattr(value, "value") else value


class GovernanceService:
    def __init__(self, client, settings: Settings, actor: str):
        self.w = client
        self.settings = settings
        self.actor = actor.strip().lower()

    def check_scope(self, catalog: str):
        if catalog not in self.settings.catalogs:
            raise GovernanceError("Catalog ngoài phạm vi GOVERNANCE_CATALOGS.")

    def check_write(self, target: Target):
        self.check_scope(target.catalog)
        if self.settings.local or self.settings.demo or not self.settings.writes:
            raise GovernanceError("Chức năng ghi đang tắt.")
        if not self.actor or self.actor not in self.settings.admins:
            raise GovernanceError("Người dùng không nằm trong GOVERNANCE_ADMIN_EMAILS.")

    def catalogs(self):
        return sorted(
            (as_dict(c) for c in self.w.catalogs.list(include_browse=True, max_results=0) if c.name in self.settings.catalogs),
            key=lambda x: x["name"],
        )

    def schemas(self, catalog):
        self.check_scope(catalog)
        return sorted((as_dict(s) for s in self.w.schemas.list(catalog_name=catalog, include_browse=True, max_results=0)), key=lambda x: x["name"])

    def objects(self, catalog, schema, kind):
        self.check_scope(catalog)
        api = {"table": self.w.tables, "function": self.w.functions}[kind]
        return sorted((as_dict(o) for o in api.list(catalog_name=catalog, schema_name=schema, max_results=0, include_browse=True)), key=lambda x: x["name"])

    def metadata(self, target):
        self.check_scope(target.catalog)
        if target.kind == "catalog":
            item = self.w.catalogs.get(name=target.full_name, include_browse=True)
        elif target.kind == "schema":
            item = self.w.schemas.get(full_name=target.full_name, include_browse=True)
        elif target.kind == "table":
            item = self.w.tables.get(full_name=target.full_name, include_browse=True)
        else:
            item = self.w.functions.get(name=target.full_name, include_browse=True)
        return as_dict(item)

    def available_privileges(self, target):
        if target.kind == "table":
            table_type = str(self.metadata(target).get("table_type", ""))
            if "VIEW" in table_type:
                return ("SELECT",)
        return PRIVILEGES[target.kind]

    def grants(self, target, effective=False):
        self.check_scope(target.catalog)
        api = self.w.grants.get_effective if effective else self.w.grants.get
        page_token, seen, result = None, set(), []
        while True:
            page = api(securable_type=target.kind, full_name=target.full_name,
                       max_results=0, page_token=page_token)
            result.extend(as_dict(x) for x in (page.privilege_assignments or []))
            page_token = page.next_page_token
            if not page_token:
                return result
            if page_token in seen:
                raise GovernanceError("API trả pagination token lặp; kết quả chưa đầy đủ.")
            seen.add(page_token)

    @staticmethod
    def direct_for(grants, principal):
        return sorted({enum_value(p) for row in grants if row.get("principal") == principal
                       for p in row.get("privileges", [])})

    @classmethod
    def fingerprint(cls, grants, principal):
        return hashlib.sha256(json.dumps(cls.direct_for(grants, principal)).encode()).hexdigest()

    def prepare(self, target, principal, action, privileges, reason):
        self.check_write(target)
        principal, reason = principal.strip(), reason.strip()
        privileges = tuple(sorted(set(privileges)))
        if not principal or any(ord(c) < 32 for c in principal):
            raise GovernanceError("Nhập principal hợp lệ: tên account group, email hoặc application ID.")
        if action not in ("grant", "revoke") or not privileges:
            raise GovernanceError("Chọn thao tác và ít nhất một quyền.")
        if not set(privileges).issubset(self.available_privileges(target)):
            raise GovernanceError("Quyền chưa được hỗ trợ cho loại đối tượng này.")
        if not reason or len(reason) > 500:
            raise GovernanceError("Nhập lý do thay đổi (1–500 ký tự).")
        grants = self.grants(target)
        current = set(self.direct_for(grants, principal))
        if action == "revoke" and not set(privileges).issubset(current):
            raise GovernanceError("Chỉ thu hồi quyền cấp trực tiếp ở đối tượng này; quyền kế thừa phải sửa tại cấp cha.")
        if action == "grant" and set(privileges).issubset(current):
            raise GovernanceError("Principal đã được cấp trực tiếp toàn bộ quyền đã chọn.")
        return Change(target, principal, action, privileges, reason, self.fingerprint(grants, principal))

    def apply(self, change: Change, confirmed_name: str):
        self.check_write(change.target)
        if confirmed_name != change.target.full_name:
            raise GovernanceError("Tên xác nhận không khớp đối tượng.")
        # Revalidate the entire action and fresh direct grants before any mutation.
        refreshed = self.prepare(change.target, change.principal, change.action, change.privileges, change.reason)
        if refreshed.before != change.before:
            raise GovernanceError("Quyền đã thay đổi sau khi xem trước. Hãy tạo bản xem trước mới.")
        values = [Privilege(p) for p in change.privileges]
        delta = PermissionsChange(principal=change.principal,
                                  add=values if change.action == "grant" else None,
                                  remove=values if change.action == "revoke" else None)
        event = {"event_id": str(uuid4()), "time_utc": datetime.now(timezone.utc).isoformat(),
                 "actor_email": self.actor, "execution_identity": "app_service_principal", **change.preview()}
        LOG.info(json.dumps({**event, "status": "requested"}, ensure_ascii=False))
        try:
            self.w.grants.update(securable_type=change.target.kind, full_name=change.target.full_name, changes=[delta])
        except Exception as exc:
            # No exception message: it may include credentials or sensitive response content.
            LOG.error(json.dumps({**event, "status": "failed_or_unknown", "error_type": type(exc).__name__}))
            raise
        event["status"] = "succeeded"
        LOG.info(json.dumps(event, ensure_ascii=False))
        return event


def grant_rows(assignments, effective=False):
    rows = []
    for a in assignments:
        for privilege in a.get("privileges", []):
            if effective:
                privilege = as_dict(privilege)
                rows.append({"Principal": a.get("principal", ""),
                             "Privilege": enum_value(privilege.get("privilege", "")),
                             "Inherited from": privilege.get("inherited_from_name", "") or "Direct",
                             "Parent type": privilege.get("inherited_from_type", "")})
            else:
                rows.append({"Principal": a.get("principal", ""), "Privilege": enum_value(privilege)})
    return rows
