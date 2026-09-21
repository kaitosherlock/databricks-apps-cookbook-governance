"""What this deployment can actually do, and why not when it cannot.

The brief asks for four distinguishable answers, never one vague "unavailable":

* ``NOT_CONFIGURED``  - the app is missing a setting (e.g. a SQL warehouse).
* ``NO_PERMISSION``   - the executing identity lacks a Unity Catalog privilege.
* ``UNSUPPORTED``     - the workspace or SDK version does not offer the API.
* ``NOT_IMPLEMENTED`` - this app has not built it.

A missing privilege is never treated as proof a feature does not exist, and an
absent API is never reported as a permission problem. Probes are lazy, cached
per identity, and read-only - probing never mutates anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .config import Settings


class State:
    AVAILABLE = "available"
    READ_ONLY = "read_only"
    NOT_CONFIGURED = "not_configured"
    NO_PERMISSION = "no_permission"
    UNSUPPORTED = "unsupported"
    NOT_IMPLEMENTED = "not_implemented"
    UNKNOWN = "unknown"

    LABELS = {
        AVAILABLE: "Sẵn sàng",
        READ_ONLY: "Chỉ đọc",
        NOT_CONFIGURED: "Chưa cấu hình",
        NO_PERMISSION: "Không đủ quyền",
        UNSUPPORTED: "Workspace chưa hỗ trợ",
        NOT_IMPLEMENTED: "Chưa triển khai",
        UNKNOWN: "Chưa kiểm tra",
    }


@dataclass
class Capability:
    key: str
    name: str
    #: Which SDK/API surface backs it - shown in the capability matrix.
    api: str
    #: Does it need a SQL warehouse to work at all?
    needs_warehouse: bool = False
    #: Does it need account-level credentials?
    needs_account: bool = False
    #: Databricks maturity as documented.
    maturity: str = "GA"
    state: str = State.UNKNOWN
    detail: str = ""
    group: str = ""

    @property
    def state_label(self) -> str:
        return State.LABELS.get(self.state, self.state)

    @property
    def usable(self) -> bool:
        return self.state in (State.AVAILABLE, State.READ_ONLY)

    def as_dict(self) -> dict:
        return {
            "Nhóm": self.group,
            "Chức năng": self.name,
            "Trạng thái": self.state_label,
            "Mức độ Databricks": self.maturity,
            "Cần SQL Warehouse": "Có" if self.needs_warehouse else "Không",
            "API": self.api,
            "Ghi chú": self.detail,
        }


#: The declared surface of this app. Every module registers here so the
#: capability screen is generated from one list rather than hand-maintained.
REGISTRY: tuple[Capability, ...] = (
    Capability("assets.browse", "Duyệt catalog / schema / tài sản", "w.catalogs, w.schemas, w.tables, w.volumes, w.functions, w.registered_models", group="Tài sản dữ liệu"),
    Capability("assets.metadata", "Xem metadata chi tiết và cột", "w.tables.get, w.functions.get, w.volumes.read", group="Tài sản dữ liệu"),
    Capability("assets.edit", "Sửa mô tả tài sản", "w.catalogs/schemas/volumes/registered_models.update", group="Tài sản dữ liệu"),
    Capability("assets.owner", "Chuyển quyền sở hữu (catalog/schema/volume/function/model)", "w.<securable>.update(owner=…)", group="Tài sản dữ liệu"),
    Capability("assets.owner_table", "Chuyển quyền sở hữu bảng / view", "SQL: ALTER TABLE … OWNER TO", needs_warehouse=True, group="Tài sản dữ liệu",
               detail="API Tables không có endpoint update; chỉ làm được bằng SQL."),

    Capability("grants.read", "Xem quyền trực tiếp", "w.grants.get", group="Quyền truy cập"),
    Capability("grants.effective", "Xem quyền hiệu lực (gồm kế thừa)", "w.grants.get_effective", group="Quyền truy cập"),
    Capability("grants.write", "Cấp và thu hồi quyền", "w.grants.update", group="Quyền truy cập"),
    Capability("principals.lookup", "Tra cứu user / group / service principal", "w.users, w.groups, w.service_principals", group="Quyền truy cập",
               detail="SCIM cấp workspace không thấy account group; xem ghi chú trong màn hình."),

    Capability("tags.read", "Xem thẻ trên tài sản", "w.entity_tag_assignments.list", group="Thẻ và phân loại"),
    Capability("tags.write", "Gán / sửa / gỡ thẻ", "w.entity_tag_assignments.create/update/delete", group="Thẻ và phân loại"),
    Capability("tags.policies", "Quản lý governed tag", "w.tag_policies.*", group="Thẻ và phân loại"),
    Capability("classification.config", "Cấu hình Data Classification theo catalog", "w.data_classification.*_catalog_config", maturity="Public Preview", group="Thẻ và phân loại"),

    Capability("policies.read", "Xem chính sách ABAC (row filter / column mask)", "w.policies.list_policies", group="Chính sách"),
    Capability("policies.write", "Tạo / sửa / xoá chính sách ABAC", "w.policies.create/update/delete_policy", group="Chính sách"),
    Capability("masks.legacy", "Xem row filter / column mask gắn trực tiếp trên bảng", "TableInfo.row_filter, ColumnInfo.mask", group="Chính sách"),

    Capability("storage.credentials", "Storage credentials", "w.storage_credentials.*", group="Lưu trữ và cách ly"),
    Capability("storage.service_credentials", "Service credentials", "w.credentials.*", group="Lưu trữ và cách ly"),
    Capability("storage.locations", "External locations", "w.external_locations.*", group="Lưu trữ và cách ly"),
    Capability("storage.bindings", "Ràng buộc workspace", "w.workspace_bindings.get_bindings/update_bindings", group="Lưu trữ và cách ly"),

    Capability("federation.connections", "Kết nối federation", "w.connections.*", group="Federation"),

    Capability("sharing.shares", "Shares", "w.shares.*", group="Chia sẻ dữ liệu"),
    Capability("sharing.recipients", "Recipients", "w.recipients.*", group="Chia sẻ dữ liệu"),
    Capability("sharing.providers", "Providers", "w.providers.*", group="Chia sẻ dữ liệu"),

    Capability("lineage.system", "Lineage bảng / cột (system tables)", "SQL: system.access.table_lineage, column_lineage", needs_warehouse=True, group="Lineage và kiểm toán"),
    Capability("lineage.external", "Lineage tới tài sản ngoài Databricks", "w.external_lineage.*", group="Lineage và kiểm toán",
               detail="Chỉ chứa quan hệ được khai báo thủ công; không phải lineage native."),
    Capability("audit.system", "Nhật ký kiểm toán Databricks", "SQL: system.access.audit", needs_warehouse=True, maturity="Public Preview", group="Lineage và kiểm toán"),

    Capability("quality.monitors", "Giám sát chất lượng dữ liệu", "w.data_quality.*", maturity="Public Preview", group="Chất lượng dữ liệu"),
    Capability("quality.constraints", "Ràng buộc bảng", "TableInfo.table_constraints", group="Chất lượng dữ liệu"),

    Capability("rfa.destinations", "Nơi nhận yêu cầu truy cập", "w.rfa.get/update_access_request_destinations", maturity="Public Preview", group="Yêu cầu truy cập"),
    Capability("rfa.submit", "Gửi yêu cầu truy cập", "w.rfa.batch_create_access_requests", maturity="Public Preview", group="Yêu cầu truy cập"),
    Capability("rfa.approve", "Phê duyệt yêu cầu truy cập", "—", group="Yêu cầu truy cập",
               detail="Databricks không cung cấp API phê duyệt. Người duyệt thao tác trong giao diện Databricks."),
)

BY_KEY = {c.key: c for c in REGISTRY}


@dataclass
class CapabilityReport:
    """Per-session capability state. Rebuilt when the identity changes."""

    settings: Settings
    items: dict[str, Capability] = field(default_factory=dict)

    def __post_init__(self):
        # Copy so probing one session never mutates the module-level registry.
        self.items = {
            c.key: Capability(**{k: v for k, v in vars(c).items()}) for c in REGISTRY
        }
        self._apply_static_rules()

    def _apply_static_rules(self):
        """Everything knowable without calling Databricks."""
        writes_on = self.settings.writes_possible
        have_warehouse = bool(self.settings.warehouse_id)

        for cap in self.items.values():
            if cap.key == "rfa.approve":
                cap.state = State.UNSUPPORTED
                cap.detail = (
                    "Databricks chưa có API liệt kê hoặc phê duyệt yêu cầu truy cập. "
                    "Ứng dụng chỉ gửi yêu cầu và ghi lại; việc duyệt thực hiện trong Databricks."
                )
                continue
            if cap.needs_warehouse and not have_warehouse:
                cap.state = State.NOT_CONFIGURED
                cap.detail = cap.detail or "Chưa đặt GOVERNANCE_WAREHOUSE_ID."
                continue
            if not writes_on and _is_write(cap.key):
                cap.state = State.READ_ONLY
                cap.detail = self.settings.write_block_reason()

    def get(self, key: str) -> Capability:
        return self.items.get(key) or Capability(key, key, "—", state=State.NOT_IMPLEMENTED)

    def usable(self, key: str) -> bool:
        return self.get(key).usable

    def mark(self, key: str, state: str, detail: str = ""):
        cap = self.items.get(key)
        if cap is None:
            return
        cap.state = state
        if detail:
            cap.detail = detail

    def probe(self, key: str, call: Callable[[], object]) -> bool:
        """Run a read-only call once and record what it proved.

        A ``PERMISSION_DENIED`` marks the capability ``NO_PERMISSION`` - it does
        **not** mark it unsupported, because a missing privilege says nothing
        about whether the feature exists.
        """
        cap = self.items.get(key)
        if cap is None:
            return False
        if cap.state in (State.NOT_CONFIGURED, State.UNSUPPORTED, State.NOT_IMPLEMENTED):
            return False
        try:
            call()
        except Exception as exc:
            from .errors import Code, translate
            err = translate(exc)
            if err.code == Code.PERMISSION_DENIED:
                cap.state = State.NO_PERMISSION
                cap.detail = "Danh tính thực thi chưa được cấp quyền Unity Catalog cần thiết."
            elif err.code == Code.CAPABILITY_UNAVAILABLE:
                cap.state = State.UNSUPPORTED
                cap.detail = "Workspace hoặc phiên bản API hiện tại không cung cấp chức năng này."
            else:
                cap.state = State.UNKNOWN
                cap.detail = f"Chưa xác định được ({err.code})."
            return False
        if cap.state == State.UNKNOWN:
            cap.state = State.READ_ONLY if not self.settings.writes_possible else State.AVAILABLE
        return True

    def rows(self) -> list[dict]:
        return [c.as_dict() for c in sorted(self.items.values(), key=lambda c: (c.group, c.name))]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cap in self.items.values():
            counts[cap.state] = counts.get(cap.state, 0) + 1
        return counts


_WRITE_SUFFIXES = ("write", "edit", "owner", "owner_table", "policies", "bindings",
                   "credentials", "service_credentials", "locations", "connections",
                   "shares", "recipients", "providers", "monitors", "destinations",
                   "submit", "config")


def _is_write(key: str) -> bool:
    tail = key.rsplit(".", 1)[-1]
    return tail in _WRITE_SUFFIXES
