"""Data quality monitors and table constraints.

Two facts shape this module:

* the current ``data_quality`` API addresses objects by **UUID**, not by name,
  so every call needs a ``table_id``/``schema_id`` lookup first;
* ``list_monitor`` is shipped but documented as *Unimplemented*, so a monitor
  inventory cannot be enumerated - it can only be probed per object. The app
  says that rather than returning an empty list that reads like "no monitors".

Monitors run on serverless compute and are billed separately, so nothing here
creates or refreshes one without an explicit, previewed action.

Constraints are classified honestly: only NOT NULL and CHECK are enforced.
PRIMARY KEY, FOREIGN KEY and UNIQUE are informational - Databricks accepts
duplicate keys and orphan rows - so a "quality score" built on a declared
primary key would be wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..authz import Action
from ..errors import Code, GovernanceError
from ..naming import Target
from ..paging import take
from ..plans import Plan, PreviewLine, fingerprint
from .base import Listing, Service, as_dict, enum_value, now_text

TABLE = "table"
SCHEMA = "schema"

COST_WARNING = (
    "Giám sát chất lượng chạy trên **serverless compute** và được tính phí riêng "
    "(SKU Data Quality Monitoring). Tạo hoặc làm mới monitor sẽ phát sinh chi phí."
)
NO_INVENTORY = (
    "Databricks chưa triển khai API liệt kê toàn bộ monitor của workspace, nên ứng dụng "
    "**không thể** kiểm kê monitor. Ứng dụng chỉ kiểm tra được từng đối tượng một. "
    "Không có monitor hiển thị ở đây **không** đồng nghĩa workspace không có monitor nào."
)
DELETE_LEAVES_ASSETS = (
    "Xoá monitor **không** xoá những gì nó đã tạo: bảng metric và dashboard vẫn còn lại. "
    "Nếu muốn dọn sạch, phải xoá các tài sản đó thủ công."
)
OBJECT_SCOPE = (
    "Giám sát bất thường áp dụng cho **schema**, còn lập hồ sơ dữ liệu áp dụng cho **bảng**. "
    "Không có loại monitor nào cho catalog, volume hay mô hình."
)
REFRESH_TABLE_ONLY = (
    "Tạo và huỷ lần làm mới chỉ áp dụng cho monitor ở cấp bảng."
)
PREVIEW_STATUS = (
    "Tính năng này đang ở giai đoạn xem trước và tài liệu Databricks chưa thống nhất mức độ "
    "(Public Preview hay Beta sau công tắc preview của workspace). Ứng dụng dò khả năng khi chạy "
    "thay vì giả định."
)
CONSTRAINT_ENFORCEMENT = (
    "Chỉ NOT NULL và CHECK được Databricks **thực thi** khi ghi. PRIMARY KEY, FOREIGN KEY và "
    "UNIQUE chỉ mang tính khai báo: Databricks vẫn chấp nhận khoá trùng và bản ghi con mồ côi. "
    "Đừng coi khoá chính đã khai báo là bảo đảm duy nhất."
)
CONSTRAINT_PROTOCOL = (
    "Thêm ràng buộc sẽ nâng writer protocol của bảng Delta nếu đang dưới phiên bản 3, có thể làm "
    "hỏng các công cụ ghi cũ bên ngoài. Trên thực tế không quay lại được."
)

ENFORCED = "enforced"
DECLARATIVE = "declarative"


@dataclass
class MonitorView:
    target: Target
    object_type: str = ""
    object_id: str = ""
    exists: bool = False
    kind: str = ""
    raw: dict = field(default_factory=dict)
    observed_at: str = ""
    error: GovernanceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def kind_label(self) -> str:
        if not self.exists:
            return "Chưa có monitor"
        return {
            "anomaly_detection": "Phát hiện bất thường (cấp schema)",
            "data_profiling": "Lập hồ sơ dữ liệu (cấp bảng)",
        }.get(self.kind, "Đã cấu hình")

    def summary_fields(self) -> list[tuple[str, str]]:
        return [
            ("Đối tượng", self.target.full_name),
            ("Loại monitor", self.kind_label),
            ("Định danh nội bộ", self.object_id or "—"),
            ("Thời điểm đọc", self.observed_at),
        ]


@dataclass
class RefreshRow:
    refresh_id: str
    state: str = ""
    trigger: str = ""
    started: str = ""
    ended: str = ""
    message: str = ""

    @property
    def state_label(self) -> str:
        cleaned = (self.state or "").replace("MONITOR_REFRESH_STATE_", "").title()
        return {
            "Success": "Thành công", "Failed": "Thất bại", "Running": "Đang chạy",
            "Pending": "Đang chờ", "Canceled": "Đã huỷ", "Unknown": "Chưa xác định",
        }.get(cleaned, cleaned or "Chưa xác định")

    def row(self) -> dict:
        return {
            "Lần chạy": self.refresh_id,
            "Trạng thái": self.state_label,
            "Kích hoạt bởi": self.trigger or "—",
            "Bắt đầu": self.started or "—",
            "Kết thúc": self.ended or "—",
            "Thông báo": self.message or "—",
        }


@dataclass
class ConstraintRow:
    name: str
    kind: str
    enforcement: str
    detail: str = ""

    @property
    def enforcement_label(self) -> str:
        return "Có hiệu lực khi ghi" if self.enforcement == ENFORCED else "Chỉ mang tính khai báo"

    def row(self) -> dict:
        return {
            "Ràng buộc": self.name or "—",
            "Loại": self.kind,
            "Hiệu lực": self.enforcement_label,
            "Chi tiết": self.detail or "—",
        }


class QualityService(Service):
    capability_keys = ("quality.monitors", "quality.constraints")

    COST_WARNING = COST_WARNING
    NO_INVENTORY = NO_INVENTORY
    DELETE_LEAVES_ASSETS = DELETE_LEAVES_ASSETS
    OBJECT_SCOPE = OBJECT_SCOPE
    REFRESH_TABLE_ONLY = REFRESH_TABLE_ONLY
    PREVIEW_STATUS = PREVIEW_STATUS
    CONSTRAINT_ENFORCEMENT = CONSTRAINT_ENFORCEMENT
    CONSTRAINT_PROTOCOL = CONSTRAINT_PROTOCOL

    def available(self) -> bool:
        return getattr(self.w, "data_quality", None) is not None

    # -- identity resolution ---------------------------------------------
    def resolve_object(self, target: Target) -> tuple[str, str]:
        """(object_type, object_id) for the current API, which takes UUIDs.

        Passing a three-level name fails, so the name is resolved to the
        securable's own id first.
        """
        self.authz.require_scope(target.catalog)
        if target.kind == "table":
            data = as_dict(self.read(lambda: self.w.tables.get(full_name=target.full_name)))
            object_id = data.get("table_id") or ""
            object_type = TABLE
        elif target.kind == "schema":
            data = as_dict(self.read(lambda: self.w.schemas.get(full_name=target.full_name)))
            object_id = data.get("schema_id") or ""
            object_type = SCHEMA
        else:
            raise GovernanceError(Code.CAPABILITY_UNAVAILABLE, OBJECT_SCOPE)
        if not object_id:
            raise GovernanceError(
                Code.NOT_FOUND,
                "Không lấy được định danh nội bộ của đối tượng, nên chưa gọi được API giám sát.",
            )
        return object_type, object_id

    # -- reads ------------------------------------------------------------
    def monitor_for(self, target: Target) -> MonitorView:
        view = MonitorView(target=target, observed_at=now_text())
        if not self.available():
            view.error = GovernanceError(
                Code.CAPABILITY_UNAVAILABLE,
                "SDK đang dùng không có API Data Quality.",
            )
            return view
        try:
            object_type, object_id = self.resolve_object(target)
        except GovernanceError as exc:
            view.error = exc
            return view
        view.object_type, view.object_id = object_type, object_id

        try:
            monitor = self.w.data_quality.get_monitor(
                object_type=object_type, object_id=object_id
            )
        except Exception as exc:
            from ..errors import translate
            err = translate(exc)
            if err.code == Code.NOT_FOUND:
                # No monitor configured is a normal state, not a failure.
                return view
            view.error = err
            return view

        data = as_dict(monitor)
        view.raw = data
        view.exists = True
        if data.get("anomaly_detection_config"):
            view.kind = "anomaly_detection"
        elif data.get("data_profiling_config"):
            view.kind = "data_profiling"
        return view

    def refreshes(self, target: Target) -> Listing:
        def run() -> Listing:
            object_type, object_id = self.resolve_object(target)
            items, more = take(
                self.w.data_quality.list_refresh(
                    object_type=object_type, object_id=object_id
                ),
                100,
            )
            rows = []
            for item in items:
                data = as_dict(item)
                rows.append(RefreshRow(
                    refresh_id=str(data.get("refresh_id") or ""),
                    state=enum_value(data.get("state")),
                    trigger=enum_value(data.get("trigger")),
                    started=_millis(data.get("start_time_ms")),
                    ended=_millis(data.get("end_time_ms")),
                    message=str(data.get("message") or "")[:200],
                ))
            return Listing(rows, "truncated" if more else "complete", now_text())

        return self.listing(run)

    def constraints(self, detail) -> list[ConstraintRow]:
        """Classify the constraints on an :class:`~ucg.services.assets.AssetDetail`."""
        rows: list[ConstraintRow] = []
        for raw in getattr(detail, "constraints", None) or []:
            item = as_dict(raw)
            primary = as_dict(item.get("primary_key_constraint") or {})
            foreign = as_dict(item.get("foreign_key_constraint") or {})
            named = as_dict(item.get("named_table_constraint") or {})
            if primary:
                rows.append(ConstraintRow(
                    name=primary.get("name") or "",
                    kind="PRIMARY KEY",
                    enforcement=DECLARATIVE,
                    detail="Cột: " + ", ".join(primary.get("child_columns") or []),
                ))
            elif foreign:
                rows.append(ConstraintRow(
                    name=foreign.get("name") or "",
                    kind="FOREIGN KEY",
                    enforcement=DECLARATIVE,
                    detail=(
                        "Cột: " + ", ".join(foreign.get("child_columns") or [])
                        + " → " + str(foreign.get("parent_table") or "")
                    ),
                ))
            elif named:
                # A named constraint here is a CHECK or NOT NULL, which Delta
                # does enforce at write time.
                rows.append(ConstraintRow(
                    name=named.get("name") or "",
                    kind="CHECK / NOT NULL",
                    enforcement=ENFORCED,
                    detail="Databricks kiểm tra ràng buộc này khi ghi dữ liệu.",
                ))
        return rows

    # -- planning ---------------------------------------------------------
    def plan_refresh(self, target: Target, reason: str) -> Plan:
        """Trigger one refresh. Costs money, so it is never automatic."""
        self.authz.require(Action.MANAGE_QUALITY, target)
        reason = _reason(reason)
        if target.kind != "table":
            raise GovernanceError(Code.CAPABILITY_UNAVAILABLE, REFRESH_TABLE_ONLY)

        view = self.monitor_for(target)
        if not view.ok:
            raise view.error
        if not view.exists:
            raise GovernanceError(
                Code.NOT_FOUND,
                "Đối tượng này chưa có monitor nào để làm mới.",
            )

        plan = self._plan(
            target, reason,
            payload={"operation": "refresh", "object_type": view.object_type,
                     "object_id": view.object_id},
            before=fingerprint({"object_id": view.object_id, "exists": True}),
            summary=f"Chạy lại giám sát chất lượng cho {target.full_name}",
        )
        plan.preview = [
            PreviewLine(f"Chạy một lần làm mới monitor cho `{target.full_name}`", "change"),
            self._identity_line(),
            PreviewLine(COST_WARNING, "warning"),
            PreviewLine(
                "Thời gian hoàn thành và chi phí cụ thể: Chưa xác định — phụ thuộc kích thước "
                "dữ liệu và cấu hình monitor.",
                "unknown",
            ),
        ]
        return plan

    def plan_delete_monitor(self, target: Target, reason: str) -> Plan:
        self.authz.require(Action.MANAGE_QUALITY, target)
        reason = _reason(reason)
        view = self.monitor_for(target)
        if not view.ok:
            raise view.error
        if not view.exists:
            raise GovernanceError(Code.NOT_FOUND, "Đối tượng này chưa có monitor.")

        plan = self._plan(
            target, reason,
            payload={"operation": "delete", "object_type": view.object_type,
                     "object_id": view.object_id},
            before=fingerprint({"object_id": view.object_id, "exists": True}),
            summary=f"Xoá monitor chất lượng của {target.full_name}",
        )
        plan.preview = [
            PreviewLine(f"Xoá cấu hình giám sát của `{target.full_name}`", "change"),
            self._identity_line(),
            PreviewLine(DELETE_LEAVES_ASSETS, "warning"),
            PreviewLine(
                "Bảng metric và dashboard còn lại sau khi xoá: Chưa xác định — API không "
                "trả về danh sách tài sản đã tạo.",
                "unknown",
            ),
        ]
        return plan

    # -- executing --------------------------------------------------------
    def apply(self, plan: Plan, target: Target, confirmation: str):
        plan.require_confirmation(confirmation)
        payload = plan.payload
        object_type, object_id = payload["object_type"], payload["object_id"]

        def revalidate() -> str:
            fresh = self.monitor_for(target)
            if not fresh.ok:
                raise fresh.error
            return fingerprint({"object_id": fresh.object_id, "exists": fresh.exists})

        if payload["operation"] == "refresh":
            do = self._refresh_call(object_type, object_id)

            def verify() -> dict:
                listing = self.refreshes(target)
                if not listing.ok:
                    raise listing.error
                latest = listing.items[0] if listing.items else None
                return {
                    "latest_refresh": latest.refresh_id if latest else "",
                    "state": latest.state_label if latest else "Chưa có",
                }
        elif payload["operation"] == "delete":
            def do():
                self.w.data_quality.delete_monitor(
                    object_type=object_type, object_id=object_id
                )

            def verify() -> dict:
                after = self.monitor_for(target)
                if not after.ok:
                    raise after.error
                return {"monitor_exists": after.exists}
        else:
            raise GovernanceError(Code.INVALID_INPUT, "Bản xem trước không hợp lệ.")

        return self.execute(plan, target, do, revalidate=revalidate, verify=verify,
                            action_label=plan.summary)

    def _refresh_call(self, object_type: str, object_id: str):
        from databricks.sdk.service.dataquality import Refresh

        def do():
            self.w.data_quality.create_refresh(
                object_type=object_type,
                object_id=object_id,
                refresh=Refresh(object_type=object_type, object_id=object_id),
            )

        return do

    # -- helpers ----------------------------------------------------------
    def _plan(self, target: Target, reason: str, *, payload: dict, before: str,
              summary: str) -> Plan:
        return Plan(
            action=Action.MANAGE_QUALITY,
            target_key=f"quality:{target.key}",
            target_name=target.full_name,
            target_type=target.label,
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload=payload,
            before=before,
            reason=reason,
            summary=summary,
        )

    def _identity_line(self) -> PreviewLine:
        from ..identity import ExecutionIdentity
        return PreviewLine(
            "Yêu cầu sẽ được gửi bằng: "
            + ExecutionIdentity.LABELS.get(self.ctx.execution_identity,
                                           self.ctx.execution_identity),
            "info",
        )


def _millis(value) -> str:
    from .base import millis_to_text
    return millis_to_text(value)


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError(Code.INVALID_INPUT, "Nhập lý do thay đổi.")
    if len(reason) > 500:
        raise GovernanceError(Code.INVALID_INPUT, "Lý do tối đa 500 ký tự.")
    return reason
