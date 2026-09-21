"""Lineage: two separate sources, neither of them complete.

Lineage is the module where an honest answer matters most, because the screen it
feeds is the one people use to decide whether dropping a table is safe. The
rules encoded here:

* **There is no supported read API for native Databricks lineage.** Table and
  column lineage can only be read as SQL against ``system.access.table_lineage``
  and ``system.access.column_lineage``, which is why this module needs a SQL
  warehouse. The ``/api/2.0/lineage-tracking/*`` endpoints the Catalog Explorer
  UI calls are internal and undocumented; this app does not touch them.
* ``w.external_lineage.*`` is a **different thing entirely**: hand-declared
  relationships to assets outside Databricks (Salesforce, Tableau, Power BI…).
  Databricks does not record those in the lineage system tables, so the two
  sources are returned as two separately labelled panels and never merged into
  one "complete" graph.
* ``event_date`` is the partition column. A query without a selective
  ``event_date`` predicate is rejected outright by the system-tables layer, so
  every statement below binds a lower bound - there is no unbounded query.
* An empty result means "nothing was recorded", **not** "nothing depends on
  this". The system tables are documented as a subset of read/write events, and
  :data:`MISSING_LINEAGE_CAUSES` lists the ways a real dependency never shows
  up. Impact analysis from this module is evidence, never proof.
* Lineage visibility follows the *executing* identity. This app usually runs as
  a service principal with broader visibility than the person looking at the
  screen, which is a privilege-escalation channel if presented carelessly -
  hence :data:`IDENTITY_WARNING` on every result.

This module is read-only by design. Unity Catalog exposes no API that writes
native lineage, and declaring external relationships is deliberately out of
scope here, so there is no plan/execute path in this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..authz import Action
from ..errors import Code, GovernanceError, translate
from ..naming import Target, validate_component
from ..paging import Completeness, take
from .base import Listing, Service, as_dict, enum_value, now_text
from .sql import SqlRunner

# -- what we read ---------------------------------------------------------
SYSTEM_CATALOG = "system"
SYSTEM_SCHEMA = "access"
TABLE_LINEAGE = "system.access.table_lineage"
COLUMN_LINEAGE = "system.access.column_lineage"

#: Time window defaults. The system tables keep a rolling year, and nothing at
#: all exists before the date below - asking for more only looks thorough.
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 365
EARLIEST_EVENT_DATE = date(2024, 9, 1)

#: Direction labels used on rows and in results.
UPSTREAM = "upstream"
DOWNSTREAM = "downstream"

DIRECTION_LABELS = {
    UPSTREAM: "Nguồn (upstream)",
    DOWNSTREAM: "Phụ thuộc (downstream)",
}


class LineageState:
    """Why a panel looks the way it does. Never one vague "không có dữ liệu"."""

    OK = "ok"
    #: Query ran, returned nothing. This is NOT proof of "no dependency".
    NOT_RECORDED = "not_recorded"
    #: Unity Catalog refused the read for the executing identity.
    NO_PERMISSION = "no_permission"
    #: The `system` schema `access` has not been enabled on the metastore.
    NOT_ENABLED = "not_enabled"
    #: No SQL warehouse configured for this deployment.
    NOT_CONFIGURED = "not_configured"
    #: Something failed and we will not guess which of the above it was.
    UNKNOWN = "unknown"

    LABELS = {
        OK: "Có dữ liệu lineage",
        NOT_RECORDED: "Không ghi nhận được lineage",
        NO_PERMISSION: "Không đủ quyền đọc system tables",
        NOT_ENABLED: "Schema system.access chưa được bật",
        NOT_CONFIGURED: "Chưa cấu hình SQL Warehouse",
        UNKNOWN: "Chưa xác định được nguyên nhân",
    }


# -- the honesty strings --------------------------------------------------
#: Shown on every lineage panel, prominently. The graph belongs to whoever ran
#: the query, not to whoever is reading it.
IDENTITY_WARNING = (
    "⚠️ Đồ thị lineage dưới đây phản ánh phạm vi nhìn thấy của **danh tính thực thi** "
    "(tài khoản đang chạy truy vấn), KHÔNG phải phạm vi của người đang xem màn hình. "
    "Nếu ứng dụng chạy bằng service principal có quyền rộng, bạn có thể đang thấy nhiều "
    "hơn những gì Unity Catalog cho phép chính bạn thấy. Đừng dùng màn hình này để suy ra "
    "quyền truy cập của bản thân."
)

#: The single most dangerous misreading of this screen.
EMPTY_IS_NOT_PROOF = (
    "Không có dòng nào KHÔNG đồng nghĩa với “không có phụ thuộc”. Databricks ghi nhận "
    "lineage dựa trên một phần (subset) các sự kiện đọc/ghi, nên nhiều phụ thuộc có thật "
    "vẫn không xuất hiện ở đây."
)

#: Enumerated so the UI can list them instead of hand-waving.
MISSING_LINEAGE_CAUSES = (
    "Truy cập qua JDBC/ODBC hoặc công cụ bên ngoài không đi qua Unity Catalog.",
    "Truy cập theo đường dẫn lưu trữ (path) thay vì theo tên ba phần.",
    "Tác vụ dùng RDD hoặc API cấp thấp thay vì DataFrame/SQL.",
    "Đổi tên bảng: việc đổi tên cắt đứt vĩnh viễn liên kết lineage đã ghi trước đó.",
    "Truy cập thông qua UDF hoặc mã tự viết mà Databricks không phân tích được.",
    "Sự kiện nằm ngoài cửa sổ thời gian đang xem hoặc ngoài thời gian lưu trữ một năm.",
    "Khối lượng công việc chạy trên cấu hình compute không phát sinh bản ghi lineage.",
)

ONE_HOP_NOTE = (
    "Kết quả chỉ đi MỘT bước (one hop) từ đối tượng đang xem. Ứng dụng không tự động "
    "duyệt tiếp các bước sau; muốn đi xa hơn, hãy mở từng đối tượng trong danh sách."
)

PATH_LIMITATION_NOTE = (
    "Bảng external được tham chiếu bằng đường dẫn lưu trữ sinh ra các dòng chỉ có "
    "source_path/target_path, còn tên ba phần để trống. Truy vấn này lọc theo tên ba "
    "phần nên có thể bỏ sót các phụ thuộc kiểu đó."
)

DIRECT_ACCESS_NOTE = (
    "Cột “Truy cập trực tiếp” = Không nghĩa là quan hệ trung gian, phát hiện khi Databricks "
    "khai triển định nghĩa view, chứ không phải tham chiếu trực tiếp. Bảng hiển thị cả hai "
    "loại và ghi nhãn rõ ràng."
)

STATEMENT_ID_NOTE = (
    "statement_id chỉ có giá trị với truy vấn chạy trên SQL Warehouse; job và pipeline để "
    "trống, nên không thể dùng cột này để ghép nối dữ liệu."
)

RECORD_ID_NOTE = (
    "record_id và event_id do hệ thống tự sinh, không ghép nối được với bất kỳ bảng nào "
    "khác; nhiều dòng có cùng một event_id là chuyện bình thường."
)

FRESHNESS_NOTE = (
    "Lineage không phải thời gian thực. Databricks không công bố cam kết về độ trễ của "
    "system tables, nên ứng dụng không hiển thị con số độ trễ nào."
)

RETENTION_NOTE = (
    f"System tables lưu cuốn chiếu khoảng một năm và không có dữ liệu nào trước ngày "
    f"{EARLIEST_EVENT_DATE.isoformat()}."
)

NO_SOURCE_ROWS_NOTE = (
    "Các sự kiện ghi không có nguồn được ghi nhận (ví dụ chèn dữ liệu từ tệp tải lên) đã "
    "bị loại khỏi bảng này vì không tạo thành một cạnh lineage."
)

EXTERNAL_LINEAGE_NOTE = (
    "Đây KHÔNG phải lineage native của Databricks. Bảng này chỉ chứa quan hệ tới tài sản "
    "ngoài Databricks do con người khai báo thủ công qua API External Lineage. Databricks "
    "không ghi các quan hệ này vào system tables, và ứng dụng không tự khai báo quan hệ nào."
)

SYSTEM_TABLE_REQUIREMENTS = (
    "Để đọc được lineage, danh tính thực thi cần: USE CATALOG trên `system`, "
    "USE SCHEMA và SELECT trên `system.access`. Ngoài ra schema `access` phải được quản trị "
    "viên account/metastore bật trước (system schema enablement). Ứng dụng chỉ phát hiện và "
    "báo cáo, không tự bật."
)

#: Attached to every result so a screen cannot show rows without the caveats.
CAVEATS: tuple[str, ...] = (
    IDENTITY_WARNING,
    EMPTY_IS_NOT_PROOF,
    ONE_HOP_NOTE,
    PATH_LIMITATION_NOTE,
    DIRECT_ACCESS_NOTE,
    FRESHNESS_NOTE,
    RETENTION_NOTE,
)

IMPACT_DISCLAIMER = (
    "Phân tích ảnh hưởng này KHÔNG đầy đủ và không được dùng làm căn cứ duy nhất để xoá, "
    "đổi tên hay thu hồi quyền trên đối tượng. Hãy đối chiếu thêm với chủ sở hữu dữ liệu, "
    "mã nguồn job/pipeline và các hệ thống tiêu thụ bên ngoài Databricks."
)


# -- SQL templates --------------------------------------------------------
# Callers never supply SQL. Every statement binds :since (partition lower bound)
# and :object_name; no value is ever concatenated in. Table and column names
# below are literals owned by this module.
#
# lower() on the name columns is deliberate: Unity Catalog treats object names
# case-insensitively, and a steward pasting `MyCatalog.Sales.Orders` should not
# silently get an empty graph.

_SQL_PROBE = f"""
SELECT 1 AS probe
FROM {TABLE_LINEAGE}
WHERE event_date >= CAST(:since AS DATE)
LIMIT 1
"""

_SQL_UPSTREAM = f"""
SELECT
    source_table_full_name AS counterpart,
    source_path            AS counterpart_path,
    source_type            AS counterpart_type,
    entity_type,
    direct_access,
    COUNT(*)                   AS event_count,
    COUNT(DISTINCT created_by) AS principal_count,
    MIN(event_time)            AS first_event_time,
    MAX(event_time)            AS last_event_time
FROM {TABLE_LINEAGE}
WHERE event_date >= CAST(:since AS DATE)
  AND lower(target_table_full_name) = lower(:object_name)
  AND (source_table_full_name IS NOT NULL OR source_path IS NOT NULL)
GROUP BY source_table_full_name, source_path, source_type, entity_type, direct_access
ORDER BY last_event_time DESC
"""

_SQL_DOWNSTREAM = f"""
SELECT
    target_table_full_name AS counterpart,
    target_path            AS counterpart_path,
    target_type            AS counterpart_type,
    entity_type,
    direct_access,
    COUNT(*)                   AS event_count,
    COUNT(DISTINCT created_by) AS principal_count,
    MIN(event_time)            AS first_event_time,
    MAX(event_time)            AS last_event_time
FROM {TABLE_LINEAGE}
WHERE event_date >= CAST(:since AS DATE)
  AND lower(source_table_full_name) = lower(:object_name)
  AND (target_table_full_name IS NOT NULL OR target_path IS NOT NULL)
GROUP BY target_table_full_name, target_path, target_type, entity_type, direct_access
ORDER BY last_event_time DESC
"""

# One statement covers both directions for a column: a steward almost always
# wants "where did this column come from, and who reads it" at the same time.
_SQL_COLUMN = f"""
SELECT
    CASE
        WHEN lower(target_table_full_name) = lower(:object_name)
             AND lower(target_column_name) = lower(:column_name)
        THEN '{UPSTREAM}' ELSE '{DOWNSTREAM}'
    END AS direction,
    source_table_full_name,
    source_column_name,
    source_path,
    source_type,
    target_table_full_name,
    target_column_name,
    target_path,
    target_type,
    entity_type,
    COUNT(*)        AS event_count,
    MIN(event_time) AS first_event_time,
    MAX(event_time) AS last_event_time
FROM {COLUMN_LINEAGE}
WHERE event_date >= CAST(:since AS DATE)
  AND (
        (lower(target_table_full_name) = lower(:object_name)
         AND lower(target_column_name) = lower(:column_name))
     OR (lower(source_table_full_name) = lower(:object_name)
         AND lower(source_column_name) = lower(:column_name))
  )
GROUP BY 1, source_table_full_name, source_column_name, source_path, source_type,
         target_table_full_name, target_column_name, target_path, target_type, entity_type
ORDER BY last_event_time DESC
"""


# -- rows -----------------------------------------------------------------
@dataclass
class LineageEdge:
    """One aggregated lineage edge, one hop from the object being examined."""

    direction: str
    counterpart: str = ""
    counterpart_path: str = ""
    counterpart_type: str = ""
    entity_type: str = ""
    #: None when the system table did not report it, which is not the same as False.
    direct_access: bool | None = None
    event_count: int = 0
    principal_count: int = 0
    first_event: str = ""
    last_event: str = ""
    source_column: str = ""
    target_column: str = ""

    @property
    def counterpart_label(self) -> str:
        if self.counterpart:
            return self.counterpart
        if self.counterpart_path:
            # Path-only rows are real lineage; they just have no UC name.
            return f"(đường dẫn) {self.counterpart_path}"
        return "(không xác định)"

    @property
    def direct_access_label(self) -> str:
        if self.direct_access is None:
            return "Chưa xác định"
        return "Có" if self.direct_access else "Không (qua khai triển view)"

    def row(self) -> dict:
        return {
            "Chiều": DIRECTION_LABELS.get(self.direction, self.direction),
            "Đối tượng liên quan": self.counterpart_label,
            "Loại": self.counterpart_type or "—",
            "Sinh ra bởi": self.entity_type or "Chưa xác định",
            "Truy cập trực tiếp": self.direct_access_label,
            "Số sự kiện": self.event_count,
            "Số principal": self.principal_count,
            "Lần đầu": self.first_event,
            "Lần gần nhất": self.last_event,
        }

    def column_row(self) -> dict:
        return {
            "Chiều": DIRECTION_LABELS.get(self.direction, self.direction),
            "Cột nguồn": self.source_column or "—",
            "Cột đích": self.target_column or "—",
            "Đối tượng liên quan": self.counterpart_label,
            "Sinh ra bởi": self.entity_type or "Chưa xác định",
            "Số sự kiện": self.event_count,
            "Lần gần nhất": self.last_event,
        }


@dataclass
class ExternalEdge:
    """A hand-declared relationship to an asset outside Databricks."""

    direction: str
    counterpart: str = ""
    counterpart_kind: str = ""
    system_type: str = ""
    entity_type: str = ""
    created_by: str = ""
    updated_at: str = ""

    def row(self) -> dict:
        return {
            "Chiều": DIRECTION_LABELS.get(self.direction, self.direction),
            "Tài sản ngoài Databricks": self.counterpart or "(không xác định)",
            "Loại đối tượng": self.counterpart_kind or "—",
            "Hệ thống": self.system_type or "Chưa khai báo",
            "Người khai báo": self.created_by or "—",
            "Cập nhật": self.updated_at or "—",
        }


# -- results --------------------------------------------------------------
@dataclass
class LineageResult(Listing):
    """A lineage panel: the rows, when they were read, and every caveat.

    Subclasses :class:`Listing` so the read contract (``items``/``completeness``/
    ``observed_at``/``error``) is the same as every other panel, while carrying
    the fields that make a lineage answer honest.
    """

    state: str = LineageState.UNKNOWN
    direction: str = ""
    source: str = "system_tables"
    target_name: str = ""
    window_days: int = DEFAULT_WINDOW_DAYS
    since: str = ""
    caveats: tuple[str, ...] = CAVEATS
    #: Extra, context-specific notes appended by the method that built this.
    notes: list[str] = field(default_factory=list)

    @property
    def state_label(self) -> str:
        return LineageState.LABELS.get(self.state, self.state)

    @property
    def has_rows(self) -> bool:
        return bool(self.items)

    @property
    def explains_emptiness(self) -> str:
        """Why the table is empty - the four cases are never collapsed into one."""
        if self.items:
            return ""
        if self.state == LineageState.NOT_RECORDED:
            return EMPTY_IS_NOT_PROOF
        if self.state == LineageState.NO_PERMISSION:
            return (
                "Không đọc được vì danh tính thực thi thiếu quyền trên system tables. "
                + SYSTEM_TABLE_REQUIREMENTS
            )
        if self.state == LineageState.NOT_ENABLED:
            return (
                "Schema `system.access` chưa được bật trên metastore này nên chưa có dữ "
                "liệu lineage để đọc. " + SYSTEM_TABLE_REQUIREMENTS
            )
        if self.state == LineageState.NOT_CONFIGURED:
            return (
                "Chức năng lineage cần một SQL Warehouse. Đặt GOVERNANCE_WAREHOUSE_ID "
                "trong cấu hình ứng dụng rồi deploy lại."
            )
        return "Chưa xác định được vì sao không có dữ liệu. Xem mã lỗi kèm theo."

    def table(self) -> list[dict]:
        return [edge.row() for edge in self.items]

    def column_table(self) -> list[dict]:
        return [edge.column_row() for edge in self.items]


@dataclass
class ImpactSummary:
    """One-hop impact evidence. Explicitly not an exhaustive answer."""

    target_name: str
    observed_at: str
    window_days: int
    downstream: LineageResult
    external: LineageResult
    #: Always False. Kept as a field so a screen cannot forget to render it.
    exhaustive: bool = False

    @property
    def direct_count(self) -> int:
        return sum(1 for e in self.downstream.items if e.direct_access is not False)

    @property
    def indirect_count(self) -> int:
        return sum(1 for e in self.downstream.items if e.direct_access is False)

    @property
    def external_count(self) -> int:
        return len(self.external.items)

    @property
    def headline(self) -> str:
        if self.downstream.state != LineageState.OK and not self.external.items:
            return f"Không ghi nhận được phụ thuộc — {self.downstream.state_label}."
        return (
            f"Ghi nhận {self.direct_count} phụ thuộc trực tiếp, {self.indirect_count} phụ "
            f"thuộc gián tiếp qua view, và {self.external_count} quan hệ tới tài sản ngoài "
            f"Databricks trong {self.window_days} ngày gần nhất."
        )

    def warnings(self) -> list[str]:
        out = [IMPACT_DISCLAIMER, IDENTITY_WARNING, EMPTY_IS_NOT_PROOF, ONE_HOP_NOTE]
        out.extend(f"Nguyên nhân lineage có thể thiếu: {cause}" for cause in MISSING_LINEAGE_CAUSES)
        out.append(PATH_LIMITATION_NOTE)
        out.append(EXTERNAL_LINEAGE_NOTE)
        out.append(FRESHNESS_NOTE)
        return out

    def rows(self) -> list[dict]:
        return self.downstream.table() + self.external.table()


@dataclass
class LineageAvailability:
    """What this deployment can actually read, and why not when it cannot."""

    system_tables: str = LineageState.UNKNOWN
    external: str = LineageState.UNKNOWN
    detail: str = ""
    external_detail: str = ""
    warehouse_configured: bool = False
    checked_at: str = ""

    @property
    def system_usable(self) -> bool:
        return self.system_tables == LineageState.OK

    @property
    def external_usable(self) -> bool:
        return self.external == LineageState.OK

    @property
    def anything_usable(self) -> bool:
        return self.system_usable or self.external_usable

    def rows(self) -> list[dict]:
        return [
            {
                "Nguồn": "Lineage bảng/cột (system.access)",
                "Trạng thái": LineageState.LABELS.get(self.system_tables, self.system_tables),
                "Ghi chú": self.detail or SYSTEM_TABLE_REQUIREMENTS,
            },
            {
                "Nguồn": "Lineage tới tài sản ngoài Databricks",
                "Trạng thái": LineageState.LABELS.get(self.external, self.external),
                "Ghi chú": self.external_detail or EXTERNAL_LINEAGE_NOTE,
            },
        ]


# -- value coercion -------------------------------------------------------
# The Statement Execution API returns every cell as a JSON string (or null), so
# nothing below trusts a Python type coming out of a row.
def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _as_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _as_bool(value) -> bool | None:
    """Tri-state: a missing flag must not be rendered as "not direct"."""
    text = _text(value).lower()
    if text in ("true", "1", "t", "yes"):
        return True
    if text in ("false", "0", "f", "no"):
        return False
    return None


def _timestamp(value) -> str:
    text = _text(value)
    if not text:
        return ""
    text = text.replace("T", " ")
    # Drop sub-second precision and the timezone suffix; minutes are enough here
    # and a full ISO string makes the table unreadable.
    if "." in text:
        text = text.split(".", 1)[0]
    return text.replace("Z", "").strip()


def _window(days: int | None) -> tuple[str, int, list[str]]:
    """Clamp the requested window and return the partition lower bound.

    Returns ``(since_iso, effective_days, notes)``. The floor at
    :data:`EARLIEST_EVENT_DATE` is what stops a "last 3 years" request from
    looking authoritative when no such data has ever existed.
    """
    notes: list[str] = []
    try:
        requested = int(days) if days else DEFAULT_WINDOW_DAYS
    except (TypeError, ValueError):
        requested = DEFAULT_WINDOW_DAYS
    if requested < 1:
        requested = DEFAULT_WINDOW_DAYS
    if requested > MAX_WINDOW_DAYS:
        notes.append(
            f"Đã rút cửa sổ thời gian về {MAX_WINDOW_DAYS} ngày: system tables chỉ lưu "
            "khoảng một năm."
        )
        requested = MAX_WINDOW_DAYS

    since = date.today() - timedelta(days=requested)
    if since < EARLIEST_EVENT_DATE:
        since = EARLIEST_EVENT_DATE
        notes.append(RETENTION_NOTE)
    return since.isoformat(), requested, notes


def _state_for_error(err: GovernanceError) -> str:
    if err.code == Code.PERMISSION_DENIED:
        return LineageState.NO_PERMISSION
    if err.code == Code.NOT_FOUND:
        return LineageState.NOT_ENABLED
    if err.code == Code.WAREHOUSE_REQUIRED:
        return LineageState.NOT_CONFIGURED
    if err.code == Code.CAPABILITY_UNAVAILABLE:
        return LineageState.NOT_ENABLED
    return LineageState.UNKNOWN


class LineageService(Service):
    """Reads lineage from the two sources Databricks actually supports."""

    capability_keys = ("lineage.system", "lineage.external")

    def __init__(self, ctx):
        super().__init__(ctx)
        self.sql = SqlRunner(self.ctx)

    # -- availability -----------------------------------------------------
    def available(self) -> LineageAvailability:
        """Probe both sources read-only and report each one separately.

        The probe never tries to enable a system schema: enabling is an
        account/metastore-admin action with metastore-wide effect, and an app
        doing it silently on someone's behalf is not acceptable.
        """
        report = LineageAvailability(
            checked_at=now_text(),
            warehouse_configured=bool(self.settings.warehouse_id),
        )

        if not report.warehouse_configured:
            report.system_tables = LineageState.NOT_CONFIGURED
            report.detail = (
                "Chưa đặt GOVERNANCE_WAREHOUSE_ID. Lineage bảng/cột chỉ đọc được bằng SQL "
                "trên system tables nên bắt buộc phải có SQL Warehouse."
            )
        else:
            report.system_tables, report.detail = self._probe_system_tables()

        report.external, report.external_detail = self._probe_external()

        # Feed the capability matrix so one screen explains the whole deployment.
        self.ctx.capabilities.mark(
            "lineage.system",
            _capability_state(report.system_tables),
            report.detail or SYSTEM_TABLE_REQUIREMENTS,
        )
        self.ctx.capabilities.mark(
            "lineage.external",
            _capability_state(report.external),
            report.external_detail or EXTERNAL_LINEAGE_NOTE,
        )
        return report

    def _probe_system_tables(self) -> tuple[str, str]:
        since, _, _ = _window(1)
        try:
            self.sql.run(_SQL_PROBE, {"since": since}, row_limit=1)
        except Exception as exc:
            err = translate(exc)
            state = _state_for_error(err)
            if state == LineageState.UNKNOWN:
                # The Statement Execution API reports a transport-level code, not
                # the SQL error class, so "table missing" and "no SELECT grant"
                # are genuinely indistinguishable here. Say so instead of picking.
                return state, (
                    "Không đọc được system.access.table_lineage và chưa phân biệt được "
                    "nguyên nhân: có thể schema `access` chưa được bật, hoặc danh tính "
                    "thực thi chưa đủ quyền. " + SYSTEM_TABLE_REQUIREMENTS
                )
            return state, err.message + " " + SYSTEM_TABLE_REQUIREMENTS
        return LineageState.OK, (
            "Đọc được system.access.table_lineage bằng danh tính thực thi hiện tại. "
            + FRESHNESS_NOTE
        )

    def _probe_external(self) -> tuple[str, str]:
        """Cheapest read on the external-lineage surface: list declared systems."""
        try:
            iterator = self.w.external_metadata.list_external_metadata()
            take(iterator, 1)
        except Exception as exc:
            err = translate(exc)
            if err.code == Code.PERMISSION_DENIED:
                return LineageState.NO_PERMISSION, (
                    "Danh tính thực thi chưa được cấp quyền đọc External Metadata."
                )
            if err.code == Code.CAPABILITY_UNAVAILABLE:
                return LineageState.NOT_ENABLED, (
                    "Workspace này chưa cung cấp API External Lineage / External Metadata."
                )
            return LineageState.UNKNOWN, "Chưa kiểm tra được API External Lineage."
        return LineageState.OK, EXTERNAL_LINEAGE_NOTE

    # -- system-table lineage ---------------------------------------------
    def upstream(self, target: Target, days: int | None = DEFAULT_WINDOW_DAYS,
                 depth_note: bool = True) -> LineageResult:
        """What this table read from, one hop back, within the window.

        ``depth_note`` attaches :data:`ONE_HOP_NOTE` to the result; a caller that
        already shows that sentence next to the panel can turn it off.
        """
        return self._table_lineage(target, days, UPSTREAM, _SQL_UPSTREAM, depth_note)

    def downstream(self, target: Target, days: int | None = DEFAULT_WINDOW_DAYS) -> LineageResult:
        """What read from this table, one hop forward, within the window."""
        return self._table_lineage(target, days, DOWNSTREAM, _SQL_DOWNSTREAM, True)

    def _table_lineage(self, target: Target, days, direction: str,
                       statement: str, depth_note: bool) -> LineageResult:
        since, window, notes = _window(days)

        def build() -> LineageResult:
            self.authz.require(Action.READ_LINEAGE, target)
            self._require_table(target)
            result = self.sql.run(statement, {"since": since, "object_name": target.full_name})
            edges = [self._edge(row, direction) for row in result.rows]
            extra = list(notes)
            extra.append(NO_SOURCE_ROWS_NOTE)
            extra.append(STATEMENT_ID_NOTE)
            if depth_note:
                extra.append(ONE_HOP_NOTE)
            return LineageResult(
                items=edges,
                completeness=Completeness.TRUNCATED if result.truncated else Completeness.COMPLETE,
                observed_at=result.observed_at or now_text(),
                note=self._note(edges, result.truncated),
                state=LineageState.OK if edges else LineageState.NOT_RECORDED,
                direction=direction,
                target_name=target.full_name,
                window_days=window,
                since=since,
                notes=extra,
            )

        return self._guarded(build, direction=direction, target_name=target.full_name,
                             window_days=window, since=since, notes=list(notes))

    def column_lineage(self, target: Target, column: str,
                       days: int | None = DEFAULT_WINDOW_DAYS) -> LineageResult:
        """Both directions for one column, one hop, within the window."""
        since, window, notes = _window(days)

        def build() -> LineageResult:
            self.authz.require(Action.READ_LINEAGE, target)
            self._require_table(target)
            # Validated for shape, then bound as a parameter - never inlined.
            name = validate_component(column, "Tên cột")
            result = self.sql.run(
                _SQL_COLUMN,
                {"since": since, "object_name": target.full_name, "column_name": name},
            )
            edges = [self._column_edge(row, target.full_name) for row in result.rows]
            extra = list(notes)
            extra.append(ONE_HOP_NOTE)
            extra.append(
                "Lineage cấp cột chỉ được ghi nhận khi Databricks phân tích được biểu thức "
                "sinh ra cột. Phép biến đổi phức tạp, UDF hoặc SELECT * động có thể làm mất "
                "liên kết cấp cột dù lineage cấp bảng vẫn có."
            )
            extra.append(RECORD_ID_NOTE)
            return LineageResult(
                items=edges,
                completeness=Completeness.TRUNCATED if result.truncated else Completeness.COMPLETE,
                observed_at=result.observed_at or now_text(),
                note=self._note(edges, result.truncated),
                state=LineageState.OK if edges else LineageState.NOT_RECORDED,
                direction="",
                target_name=f"{target.full_name}.{name}",
                window_days=window,
                since=since,
                notes=extra,
            )

        return self._guarded(build, direction="", target_name=target.full_name,
                             window_days=window, since=since, notes=list(notes))

    # -- external lineage --------------------------------------------------
    def external_relationships(self, target: Target) -> LineageResult:
        """Hand-declared relationships to assets outside Databricks.

        Separate panel, separate label: these relationships are not in the
        lineage system tables and the system tables are not in here.
        """

        def build() -> LineageResult:
            self.authz.require(Action.READ_LINEAGE, target)
            self._require_table(target)
            edges: list[ExternalEdge] = []
            truncated = False
            for direction in (UPSTREAM, DOWNSTREAM):
                page, more = self._external_page(target, direction)
                edges.extend(page)
                truncated = truncated or more
            return LineageResult(
                items=edges,
                completeness=Completeness.TRUNCATED if truncated else Completeness.COMPLETE,
                observed_at=now_text(),
                note=EXTERNAL_LINEAGE_NOTE,
                state=LineageState.OK if edges else LineageState.NOT_RECORDED,
                source="external_lineage",
                target_name=target.full_name,
                caveats=(IDENTITY_WARNING, EXTERNAL_LINEAGE_NOTE, ONE_HOP_NOTE),
                notes=[
                    EXTERNAL_LINEAGE_NOTE,
                    "Quan hệ ngoài Databricks phải được khai báo thủ công; không có quan hệ "
                    "nào không đồng nghĩa với không có hệ thống nào đang dùng dữ liệu này.",
                ],
            )

        return self._guarded(build, target_name=target.full_name, source="external_lineage")

    def _external_page(self, target: Target, direction: str) -> tuple[list[ExternalEdge], bool]:
        from databricks.sdk.service.catalog import ExternalLineageObject, LineageDirection

        wanted = LineageDirection.UPSTREAM if direction == UPSTREAM else LineageDirection.DOWNSTREAM
        # Built through from_dict so this keeps working regardless of what the
        # generated nested dataclass calls its name field; unknown keys are
        # dropped by the SDK deserialiser rather than raising.
        object_info = ExternalLineageObject.from_dict(
            {"table": {"name": target.full_name, "full_name": target.full_name}}
        )
        iterator = self.w.external_lineage.list_external_lineage_relationships(
            object_info=object_info, lineage_direction=wanted,
        )
        items, more = take(iterator, self.settings.page_size)
        return [self._external_edge(item, direction) for item in items], more

    def external_systems(self) -> Listing:
        """External metadata objects declared on this metastore.

        Useful context for the external panel: it names the systems someone has
        registered, so "no relationship" can be read against "no system declared".
        """

        def build() -> Listing:
            self.authz.require(Action.READ_LINEAGE, None)
            items, more = take(
                self.w.external_metadata.list_external_metadata(), self.settings.page_size
            )
            rows = []
            for item in items:
                data = as_dict(item)
                rows.append({
                    "Tên": _text(data.get("name")),
                    "Hệ thống": enum_value(data.get("system_type")) or "Chưa khai báo",
                    "Loại thực thể": _text(data.get("entity_type")),
                    "Mô tả": _text(data.get("description")),
                    "Người tạo": _text(data.get("owner")) or _text(data.get("created_by")),
                })
            return Listing(
                items=rows,
                completeness=Completeness.TRUNCATED if more else Completeness.COMPLETE,
                observed_at=now_text(),
                note=EXTERNAL_LINEAGE_NOTE,
            )

        return self.listing(build)

    # -- impact ------------------------------------------------------------
    def impact_summary(self, target: Target,
                       days: int | None = DEFAULT_WINDOW_DAYS) -> ImpactSummary:
        """Evidence for "what breaks if this changes" - never a verdict.

        Both panels are collected even when one fails, because "downstream could
        not be read" and "downstream is empty" must not look the same.
        """
        _, window, _ = _window(days)
        return ImpactSummary(
            target_name=target.full_name,
            observed_at=now_text(),
            window_days=window,
            downstream=self.downstream(target, days),
            external=self.external_relationships(target),
        )

    # -- plumbing ----------------------------------------------------------
    def _require_table(self, target: Target):
        if target.kind != "table":
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Lineage chỉ tra cứu được cho bảng hoặc view (tên ba phần). "
                f"Đối tượng đang chọn là: {target.label}.",
            )

    def _guarded(self, build, **meta) -> LineageResult:
        """Run a lineage read through the shared listing guard.

        ``Service.listing`` returns a plain ``Listing`` on failure, so the error
        is re-wrapped here to keep the lineage-specific fields - including the
        state that tells a permission problem apart from an empty graph.
        """
        out = self.listing(build)
        if isinstance(out, LineageResult):
            return out
        err = out.error
        return LineageResult(
            items=[],
            completeness=out.completeness,
            observed_at=out.observed_at or now_text(),
            note=err.message if err else "",
            error=err,
            state=_state_for_error(err) if err else LineageState.UNKNOWN,
            **meta,
        )

    @staticmethod
    def _note(edges: list, truncated: bool) -> str:
        if truncated:
            return (
                "Kết quả đã bị cắt bớt vì vượt giới hạn dòng. Hãy thu hẹp cửa sổ thời gian "
                "để xem đầy đủ hơn."
            )
        if not edges:
            return EMPTY_IS_NOT_PROOF
        return ONE_HOP_NOTE

    @staticmethod
    def _edge(row: dict, direction: str) -> LineageEdge:
        return LineageEdge(
            direction=direction,
            counterpart=_text(row.get("counterpart")),
            counterpart_path=_text(row.get("counterpart_path")),
            counterpart_type=_text(row.get("counterpart_type")),
            entity_type=_text(row.get("entity_type")),
            direct_access=_as_bool(row.get("direct_access")),
            event_count=_as_int(row.get("event_count")),
            principal_count=_as_int(row.get("principal_count")),
            first_event=_timestamp(row.get("first_event_time")),
            last_event=_timestamp(row.get("last_event_time")),
        )

    @staticmethod
    def _column_edge(row: dict, object_name: str) -> LineageEdge:
        direction = _text(row.get("direction")) or DOWNSTREAM
        # For an upstream row the counterpart is the source side, and vice versa.
        if direction == UPSTREAM:
            counterpart = _text(row.get("source_table_full_name"))
            counterpart_path = _text(row.get("source_path"))
            counterpart_type = _text(row.get("source_type"))
        else:
            counterpart = _text(row.get("target_table_full_name"))
            counterpart_path = _text(row.get("target_path"))
            counterpart_type = _text(row.get("target_type"))
        return LineageEdge(
            direction=direction,
            counterpart=counterpart or ("" if counterpart_path else object_name),
            counterpart_path=counterpart_path,
            counterpart_type=counterpart_type,
            entity_type=_text(row.get("entity_type")),
            event_count=_as_int(row.get("event_count")),
            first_event=_timestamp(row.get("first_event_time")),
            last_event=_timestamp(row.get("last_event_time")),
            source_column=_text(row.get("source_column_name")),
            target_column=_text(row.get("target_column_name")),
        )

    @staticmethod
    def _external_edge(item, direction: str) -> ExternalEdge:
        data = as_dict(item)
        # The counterpart sits under one of several keys depending on what kind
        # of object was declared; only one of them is ever populated.
        info = as_dict(data.get("external_lineage_info"))
        counterpart = ""
        kind = ""
        for key, label in (
            ("external_metadata_info", "Tài sản ngoài Databricks"),
            ("external_metadata", "Tài sản ngoài Databricks"),
            ("path_info", "Đường dẫn lưu trữ"),
            ("path", "Đường dẫn lưu trữ"),
            ("table_info", "Bảng Unity Catalog"),
            ("table", "Bảng Unity Catalog"),
            ("model_version_info", "Phiên bản mô hình"),
            ("model_version", "Phiên bản mô hình"),
        ):
            raw = data.get(key)
            if not raw:
                continue
            nested = as_dict(raw)
            counterpart = (
                _text(nested.get("name"))
                or _text(nested.get("full_name"))
                or _text(nested.get("path"))
                or _text(nested.get("url"))
                or _text(raw if isinstance(raw, str) else "")
            )
            kind = label
            if counterpart:
                break
        return ExternalEdge(
            direction=direction,
            counterpart=counterpart,
            counterpart_kind=kind,
            system_type=enum_value(
                as_dict(data.get("external_metadata_info") or data.get("external_metadata")).get("system_type")
            ),
            entity_type=_text(info.get("entity_type")) or _text(data.get("entity_type")),
            created_by=_text(info.get("created_by")) or _text(data.get("created_by")),
            updated_at=_timestamp(info.get("updated_at") or data.get("updated_at")),
        )


def _capability_state(state: str) -> str:
    """Map a lineage state onto the shared capability vocabulary."""
    from ..capabilities import State

    return {
        LineageState.OK: State.READ_ONLY,
        LineageState.NOT_RECORDED: State.READ_ONLY,
        LineageState.NO_PERMISSION: State.NO_PERMISSION,
        LineageState.NOT_ENABLED: State.UNSUPPORTED,
        LineageState.NOT_CONFIGURED: State.NOT_CONFIGURED,
    }.get(state, State.UNKNOWN)
