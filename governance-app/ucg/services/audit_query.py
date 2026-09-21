"""Reading Databricks' own audit records, and honest governance review checks.

Two different records get confused constantly, so they are kept apart by name:

* :mod:`ucg.audit` is **this app's operation log** - what this app did, in this
  session, written to stdout. It disappears with the compute.
* This module reads **Databricks' audit record**, the system table
  ``system.access.audit``. It is the authoritative one, and this app only ever
  reads it.

The correctness rules encoded here, all of which are easy to get wrong:

* ``event_date`` is the **partition column**. Databricks rejects a query that
  does not bind a selective ``event_date`` range, so every statement below binds
  one and the window is both defaulted short and capped.
* Unity Catalog audit events are **account-level**: the top-level
  ``workspace_id`` column is ``0`` and the originating workspace sits in
  ``request_params.workspace_id``. ``WHERE service_name='unityCatalog' AND
  workspace_id=<id>`` therefore returns zero rows - a silent empty result that
  looks exactly like "nothing happened".
* ``user_identity.email`` is who *asked*; ``identity_metadata.run_as`` is who
  the workload *ran as*. Attributing on the first alone misattributes every
  access performed by a job or a service principal, so both are surfaced and
  every filter records which field it used.
* Four ``request_params`` keys are **silently omitted** - not errored - for
  callers who are neither account admins nor members of ``databricks_pii_access``.
* Columns and struct fields can appear at any time, so nothing here selects
  ``*`` into a fixed shape; every column is named.

The review checks at the bottom deliberately run on data passed in from other
services rather than querying anything undocumented, and they produce *findings*
- signals to review - never compliance verdicts, and never an automatic change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from .. import privileges as priv
from ..authz import Action
from ..capabilities import State
from ..errors import Code, GovernanceError
from ..naming import Target
from ..paging import Completeness
from .base import Listing, Service, now_text
from .sql import MAX_ROWS as SQL_MAX_ROWS
from .sql import SqlRunner

# -- the source ------------------------------------------------------------

#: The one table this module reads. Public Preview at Databricks.
AUDIT_TABLE = "system.access.audit"
SYSTEM_CATALOG = "system"
SYSTEM_SCHEMA = "access"

#: ``service_name`` for Unity Catalog events. Account-level; see WORKSPACE_NOTE.
UC_SERVICE_NAME = "unityCatalog"

#: What the executing identity must already have. This app never enables the
#: system schema itself: that is an account/metastore admin action by design.
REQUIREMENTS = (
    "Một SQL Warehouse đang chạy (GOVERNANCE_WAREHOUSE_ID).",
    "Quyền USE CATALOG trên `system`.",
    "Quyền USE SCHEMA và SELECT trên `system.access`.",
    "Schema hệ thống `access` đã được quản trị viên account/metastore BẬT. "
    "Ứng dụng này không tự bật và không thể tự bật.",
)

# -- the time window -------------------------------------------------------

#: Short by default: the table is large and partitioned by day.
DEFAULT_WINDOW_DAYS = 7
#: Hard ceiling per query. A longer look-back is done as several queries.
MAX_WINDOW_DAYS = 30
#: Databricks keeps audit rows for a rolling year.
RETENTION_DAYS = 365
#: Rows belonging to a deleted workspace disappear this long after deletion.
DELETED_WORKSPACE_GRACE_DAYS = 14

DEFAULT_ROW_LIMIT = 200

# -- the notes that must travel with every result --------------------------

WORKSPACE_NOTE = (
    "Sự kiện Unity Catalog là sự kiện cấp ACCOUNT: cột `workspace_id` ở mức trên "
    "cùng bằng 0, workspace thực sự nằm trong `request_params.workspace_id`. "
    "Vì vậy truy vấn ở đây KHÔNG lọc theo `workspace_id` mức trên cùng — lọc như vậy "
    "sẽ trả về 0 dòng và trông y hệt “không có sự kiện nào”."
)

IDENTITY_NOTE = (
    "“Người khởi tạo” (`user_identity.email`) là danh tính phát ra yêu cầu. "
    "“Danh tính thực thi” (`identity_metadata.run_as`) là danh tính mà workload thực sự "
    "chạy dưới đó. Hai cột này khác nhau với job và service principal; quy trách nhiệm "
    "chỉ dựa vào một cột là sai."
)

#: request_params keys Databricks drops without saying so.
REDACTED_REQUEST_PARAMS = (
    "function_info",
    "view_definition",
    "definition_json",
    "managed_definition",
)

REDACTION_NOTE = (
    "Bốn khoá trong `request_params` — "
    + ", ".join(f"`{k}`" for k in REDACTED_REQUEST_PARAMS)
    + " — bị LƯỢC BỎ âm thầm (không báo lỗi) nếu người gọi không phải account admin "
    "và không thuộc nhóm `databricks_pii_access`. Thiếu các trường này KHÔNG có nghĩa "
    "là sự kiện không có định nghĩa tương ứng."
)

RETENTION_NOTE = (
    f"Databricks giữ bản ghi kiểm toán trong {RETENTION_DAYS} ngày gần nhất (cuốn chiếu). "
    f"Sự kiện của một workspace đã bị xoá sẽ biến mất sau {DELETED_WORKSPACE_GRACE_DAYS} ngày."
)

SCHEMA_DRIFT_NOTE = (
    "Databricks có thể bổ sung cột hoặc trường struct mới bất cứ lúc nào. "
    "Ứng dụng chỉ đọc các cột được nêu tên, nên cột mới sẽ không tự xuất hiện ở đây."
)

TIMEZONE_NOTE = (
    "Cửa sổ thời gian tính theo `event_date` (ngày UTC). Sự kiện sát ranh giới ngày "
    "có thể nằm ở phân vùng liền trước hoặc liền sau so với giờ địa phương."
)

OBJECT_MATCH_NOTE = (
    "Không phải hành động nào cũng ghi tên đối tượng vào cùng một khoá của `request_params`. "
    "Bộ lọc theo đối tượng đối chiếu vài khoá phổ biến nhất, nên có thể BỎ SÓT dòng hợp lệ."
)

PREVIEW_NOTE = (
    f"`{AUDIT_TABLE}` đang ở mức Public Preview: cấu trúc và độ trễ ghi nhận có thể thay đổi."
)

CORRELATION_NOTE = (
    "Nhật ký thao tác của ứng dụng (`ucg.audit`) và bản ghi kiểm toán của Databricks là "
    "HAI bản ghi độc lập. Ứng dụng sinh `operation_id`/`event_id` riêng và Databricks KHÔNG "
    "ghi lại các mã đó. Việc đối chiếu chỉ là GẦN ĐÚNG, dựa trên (khoảng thời gian, danh tính, "
    "đối tượng, hành động). Không được trình bày kết quả đối chiếu như một phép nối (join) "
    "chính xác hay như bằng chứng."
)

#: Shown under every audit table.
AUDIT_CAVEAT = (
    "Bảng này là bản ghi kiểm toán của Databricks, không phải nhật ký thao tác của ứng dụng. "
    "Kết quả chỉ gồm những gì danh tính thực thi được phép đọc, trong cửa sổ thời gian đã chọn."
)

#: How long a "best-effort" correlation window is allowed to be.
CORRELATION_WINDOW_MINUTES = 10


# -- one row ---------------------------------------------------------------


@dataclass
class AuditRow:
    """One audit record, reduced to the columns this app names explicitly."""

    event_time: str = ""
    event_date: str = ""
    service_name: str = ""
    action_name: str = ""
    #: user_identity.email - who asked.
    initiator: str = ""
    #: identity_metadata.run_as - who it ran as.
    run_as: str = ""
    #: request_params.workspace_id - the real workspace for UC events.
    source_workspace_id: str = ""
    #: The top-level column. 0 for account-level events; kept so the UI can show why.
    record_workspace_id: str = ""
    object_name: str = ""
    securable_type: str = ""
    status_code: str = ""
    audit_level: str = ""
    event_id: str = ""
    source_ip: str = ""

    @property
    def account_level(self) -> bool:
        """True when the row carries no meaningful top-level workspace id."""
        return str(self.record_workspace_id or "0").strip() in ("", "0")

    @property
    def executed_by(self) -> str:
        """The identity that actually performed the access, when it is known."""
        return self.run_as or self.initiator

    def row(self) -> dict:
        return {
            "Thời điểm (UTC)": (self.event_time or "").replace("T", " ")[:19] or "—",
            "Dịch vụ": self.service_name or "—",
            "Hành động": self.action_name or "—",
            "Người khởi tạo": self.initiator or "Chưa xác định",
            "Danh tính thực thi": self.run_as or "Không ghi nhận",
            "Workspace phát sinh": self.source_workspace_id or (
                "Sự kiện cấp account" if self.account_level else self.record_workspace_id
            ),
            "Đối tượng": self.object_name or "—",
            "Loại securable": self.securable_type or "—",
            "Mã trạng thái": str(self.status_code or "—"),
            "Event ID": (self.event_id or "")[:12] or "—",
        }


@dataclass
class AuditPage:
    """A window of audit rows, with the honesty fields attached.

    ``completeness`` distinguishes the three answers that must never be blurred:
    complete, cut short by a limit, or missing data because of permissions.
    An empty ``rows`` with ``completeness == COMPLETE`` means *khong co du lieu*;
    ``PARTIAL_PERMISSION`` means *khong du quyen*.
    """

    rows: list[AuditRow] = field(default_factory=list)
    observed_at: str = ""
    completeness: str = Completeness.COMPLETE
    caveats: list[str] = field(default_factory=list)
    #: The bound event_date range, as ISO dates.
    window_start: str = ""
    window_end: str = ""
    #: Which filters were applied, in plain terms - including which identity
    #: column an actor filter used.
    filters: dict = field(default_factory=dict)
    truncated: bool = False
    error: GovernanceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def empty(self) -> bool:
        return not self.rows

    @property
    def no_data_reason(self) -> str:
        """Why the page is empty, said out loud rather than left to guess."""
        if not self.empty:
            return ""
        if self.error is not None:
            return f"Không đọc được dữ liệu: {self.error.message}"
        if self.completeness == Completeness.PARTIAL_PERMISSION:
            return (
                "Thiếu dữ liệu do không đủ quyền trên `system.access`. "
                "Đây KHÔNG phải kết luận “không có sự kiện nào”."
            )
        return (
            f"Không có bản ghi nào trong khoảng {self.window_start} → {self.window_end}. "
            "Đây là “không có dữ liệu”, không phải “không đủ quyền”."
        )

    def table(self) -> list[dict]:
        return [r.row() for r in self.rows]

    def as_listing(self) -> Listing:
        """Same content in the shape a generic panel expects."""
        return Listing(
            items=list(self.rows),
            completeness=self.completeness,
            observed_at=self.observed_at,
            note=AUDIT_CAVEAT,
            error=self.error,
        )


@dataclass
class AuditAvailability:
    """Whether this deployment can read the audit table at all, and why not."""

    state: str
    message: str
    checked_at: str = ""
    detail: str = ""
    requirements: tuple[str, ...] = REQUIREMENTS

    @property
    def usable(self) -> bool:
        return self.state in (State.AVAILABLE, State.READ_ONLY)

    @property
    def state_label(self) -> str:
        return State.LABELS.get(self.state, self.state)


# -- governance review checks ----------------------------------------------


class Severity:
    """How loudly a finding asks to be looked at. Never a verdict."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    LABELS = {
        HIGH: "Cần xem xét sớm",
        MEDIUM: "Nên xem xét",
        LOW: "Ghi nhận để tham khảo",
    }


#: Principal names that mean "effectively everyone in the account".
BROAD_PRINCIPALS = frozenset({
    "account users",
    "all account users",
    "all workspace users",
    "users",
})

FINDING_DISCLAIMER = (
    "Phát hiện rà soát là TÍN HIỆU CẦN XEM XÉT, không phải kết luận vi phạm tuân thủ. "
    "Một phát hiện có thể hoàn toàn hợp lệ trong bối cảnh của bạn. Ứng dụng KHÔNG bao giờ "
    "tự động thay đổi quyền hay cấu hình dựa trên các phát hiện này; mọi thay đổi vẫn phải "
    "đi qua quy trình xem trước và xác nhận."
)

RULE_MISSING_OWNER = "GOV-001"
RULE_MISSING_DESCRIPTION = "GOV-002"
RULE_BROAD_PRINCIPAL = "GOV-003"
RULE_BROAD_PRIVILEGE = "GOV-004"
RULE_EXTERNAL_SHARING = "GOV-005"

#: rule id -> (title, severity, what the rule actually looks at).
RULES: dict[str, tuple[str, str, str]] = {
    RULE_MISSING_OWNER: (
        "Tài sản chưa có chủ sở hữu rõ ràng",
        Severity.HIGH,
        "Đối tượng không có giá trị `owner` trong metadata Unity Catalog.",
    ),
    RULE_MISSING_DESCRIPTION: (
        "Tài sản chưa có mô tả",
        Severity.LOW,
        "Đối tượng không có `comment`, khiến người dùng khó biết dữ liệu này là gì.",
    ),
    RULE_BROAD_PRINCIPAL: (
        "Quyền được cấp cho nhóm có phạm vi rất rộng",
        Severity.HIGH,
        "Principal nhận quyền là nhóm bao trùm gần như toàn bộ account.",
    ),
    RULE_BROAD_PRIVILEGE: (
        "Quyền có phạm vi rộng hơn nhiều so với đối tượng",
        Severity.HIGH,
        "Quyền thuộc nhóm ucg.privileges.BROAD (ví dụ ALL_PRIVILEGES, MANAGE).",
    ),
    RULE_EXTERNAL_SHARING: (
        "Đối tượng đang được chia sẻ ra ngoài",
        Severity.MEDIUM,
        "Đối tượng xuất hiện trong dữ liệu chia sẻ do màn hình Chia sẻ dữ liệu cung cấp.",
    ),
}


@dataclass
class ReviewInput:
    """Data the review checks run on.

    Nothing here is queried by this module: the caller passes in what the other
    services already read, so a check can never claim coverage the app did not
    actually have. ``completeness`` is inherited from those reads.
    """

    #: AssetRef/AssetDetail-like objects, or dicts with the same field names.
    assets: Sequence[Any] = ()
    #: Dicts: {"object", "principal", "privilege", "source"}.
    grants: Sequence[Any] = ()
    #: Dicts from SharingService: {"object", "share", "recipient", "detail"}.
    external_shares: Sequence[Any] = ()

    asset_source: str = "Unity Catalog API (catalogs / schemas / tables / volumes / functions)"
    grant_source: str = "Unity Catalog API (grants.get)"
    sharing_source: str = "Delta Sharing API (shares / recipients)"

    #: Completeness of the underlying reads, from paging.Completeness.
    completeness: str = Completeness.COMPLETE
    #: What was in scope when the caller collected the data.
    scope_note: str = ""


@dataclass
class Finding:
    """One rule's result. Always carries how much it could actually see."""

    rule_id: str
    title: str
    severity: str
    rows: list[dict]
    source: str
    observed_at: str
    completeness: str
    caveat: str
    #: How many input records the rule examined. 0 rows out of 0 examined is
    #: "chua ra soat gi", not "khong co van de".
    examined: int = 0

    @property
    def severity_label(self) -> str:
        return Severity.LABELS.get(self.severity, self.severity)

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def conclusion(self) -> str:
        if self.examined == 0:
            return (
                "Chưa rà soát bản ghi nào: không có dữ liệu đầu vào. "
                "Kết quả trống ở đây KHÔNG có nghĩa là không có vấn đề."
            )
        if not self.rows:
            return f"Không có phát hiện nào trong {self.examined} bản ghi đã rà soát."
        return f"{self.count} phát hiện trên {self.examined} bản ghi đã rà soát."

    def as_dict(self) -> dict:
        return {
            "Mã quy tắc": self.rule_id,
            "Nội dung rà soát": self.title,
            "Mức độ": self.severity_label,
            "Số phát hiện": self.count,
            "Đã rà soát": self.examined,
            "Nguồn dữ liệu": self.source,
            "Thời điểm quan sát": self.observed_at,
            "Giới hạn dữ liệu": self.caveat,
        }


@dataclass
class ReviewReport:
    """All findings from one review pass, plus what the pass could not see."""

    findings: list[Finding] = field(default_factory=list)
    observed_at: str = ""
    completeness: str = Completeness.COMPLETE
    scope_note: str = ""
    disclaimer: str = FINDING_DISCLAIMER

    @property
    def total(self) -> int:
        return sum(f.count for f in self.findings)

    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def summary_rows(self) -> list[dict]:
        return [f.as_dict() for f in self.findings]


# -- SQL -------------------------------------------------------------------

# Named columns only: selecting * would break the moment Databricks adds a
# column or a struct field, which it may do at any time.
_COLUMNS = """
    event_time,
    event_date,
    service_name,
    action_name,
    user_identity.email                AS initiator,
    identity_metadata.run_as           AS run_as,
    request_params.workspace_id        AS source_workspace_id,
    workspace_id                       AS record_workspace_id,
    coalesce(
        request_params.full_name_arg,
        request_params.name_arg,
        request_params.table_full_name,
        request_params.securable_full_name
    )                                  AS object_name,
    request_params.securable_type      AS securable_type,
    response.status_code               AS status_code,
    audit_level,
    event_id,
    source_ip_address                  AS source_ip
"""

# `response.error_message` is deliberately not selected: it is upstream response
# content and can carry values that must not end up in this app's output.

# to_date(:param) rather than a bare comparison: parameter markers arrive as
# STRING, and an explicit cast keeps the predicate a foldable constant, which is
# what lets Databricks prune the event_date partitions instead of rejecting the
# query.
_WINDOW = "event_date >= to_date(:since) AND event_date <= to_date(:until)"

_PROBE_SQL = (
    f"SELECT event_date FROM {AUDIT_TABLE} WHERE {_WINDOW} LIMIT 1"
)

_ACTOR_PREDICATES = {
    "initiator": "lower(user_identity.email) = lower(:actor)",
    "run_as": "lower(identity_metadata.run_as) = lower(:actor)",
    "both": (
        "(lower(user_identity.email) = lower(:actor)"
        " OR lower(identity_metadata.run_as) = lower(:actor))"
    ),
}

ACTOR_FIELD_LABELS = {
    "initiator": "Người khởi tạo (user_identity.email)",
    "run_as": "Danh tính thực thi (identity_metadata.run_as)",
    "both": "Cả người khởi tạo và danh tính thực thi",
}

_OBJECT_PREDICATE = """
    lower(coalesce(
        request_params.full_name_arg,
        request_params.name_arg,
        request_params.table_full_name,
        request_params.securable_full_name,
        ''
    )) = lower(:object_name)
"""


class AuditService(Service):
    """Reads ``system.access.audit`` and runs rule-based governance reviews."""

    capability_keys = ("audit.system",)

    def __init__(self, ctx):
        super().__init__(ctx)
        # The audit table has no REST equivalent, so this is one of the few
        # modules that needs a warehouse.
        self.runner = SqlRunner(self.ctx)

    # -- availability -----------------------------------------------------
    def available(self) -> AuditAvailability:
        """Can this deployment read the audit table, and if not, exactly why?

        The four answers are kept apart: no warehouse configured, no Unity
        Catalog privilege, the system schema not enabled / API absent, and
        "readable but empty in this window".
        """
        self.authz.require(Action.READ_AUDIT, None)
        checked_at = now_text()

        if not self.runner.available():
            return AuditAvailability(
                State.NOT_CONFIGURED,
                "Chưa cấu hình SQL Warehouse. Đặt GOVERNANCE_WAREHOUSE_ID rồi deploy lại "
                "ứng dụng để đọc được nhật ký kiểm toán của Databricks.",
                checked_at,
            )

        start, end, _ = self._window(DEFAULT_WINDOW_DAYS)
        try:
            result = self.runner.run(
                _PROBE_SQL,
                {"since": start.isoformat(), "until": end.isoformat()},
                row_limit=1,
            )
        except GovernanceError as err:
            if err.code == Code.PERMISSION_DENIED:
                return AuditAvailability(
                    State.NO_PERMISSION,
                    "Danh tính thực thi chưa đủ quyền đọc `system.access.audit`. "
                    "Cần USE CATALOG trên `system`, USE SCHEMA và SELECT trên `system.access`.",
                    checked_at,
                    detail=err.code,
                )
            if err.code in (Code.NOT_FOUND, Code.CAPABILITY_UNAVAILABLE):
                return AuditAvailability(
                    State.UNSUPPORTED,
                    "Không tìm thấy bảng `system.access.audit`. Schema hệ thống `access` "
                    "nhiều khả năng chưa được quản trị viên account/metastore bật. "
                    "Ứng dụng không tự bật schema hệ thống.",
                    checked_at,
                    detail=err.code,
                )
            return AuditAvailability(
                State.UNKNOWN,
                "Chưa xác định được khả năng đọc nhật ký kiểm toán. "
                + (err.next_step or "Thử lại sau ít phút."),
                checked_at,
                detail=err.code,
            )

        if result.empty:
            # Readable but nothing in the probe window: that is data, not a
            # permission problem, and the two must not be reported the same way.
            return AuditAvailability(
                State.READ_ONLY,
                f"Đọc được `{AUDIT_TABLE}`, nhưng không có bản ghi nào trong "
                f"{DEFAULT_WINDOW_DAYS} ngày gần nhất. Đây là “không có dữ liệu”, "
                "không phải “không đủ quyền”.",
                checked_at,
            )
        return AuditAvailability(
            State.READ_ONLY,
            f"Đọc được `{AUDIT_TABLE}`. {PREVIEW_NOTE}",
            checked_at,
        )

    # -- queries ----------------------------------------------------------
    def recent(
        self,
        days: int = DEFAULT_WINDOW_DAYS,
        actor: str | None = None,
        action: str | None = None,
        object_name: str | None = None,
        *,
        actor_field: str = "both",
        service: str | None = None,
        limit: int = DEFAULT_ROW_LIMIT,
    ) -> AuditPage:
        """Recent audit events, optionally narrowed.

        ``actor_field`` chooses which identity column the actor filter applies
        to; whichever is used is recorded in :attr:`AuditPage.filters`, because
        "who did this" has two different correct answers.
        """
        self.authz.require(Action.READ_AUDIT, None)

        actor = (actor or "").strip()
        action = (action or "").strip()
        object_name = (object_name or "").strip()
        service = (service or "").strip()
        if actor_field not in _ACTOR_PREDICATES:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Trường danh tính để lọc phải là một trong: "
                + ", ".join(sorted(_ACTOR_PREDICATES)),
            )

        predicates: list[str] = []
        params: dict[str, str] = {}
        filters: dict[str, str] = {}

        if actor:
            predicates.append(_ACTOR_PREDICATES[actor_field])
            params["actor"] = actor
            filters["Danh tính"] = actor
            filters["Lọc theo cột"] = ACTOR_FIELD_LABELS[actor_field]
        if action:
            predicates.append("action_name = :action_name")
            params["action_name"] = action
            filters["Hành động"] = action
        if object_name:
            predicates.append(_OBJECT_PREDICATE)
            params["object_name"] = object_name
            filters["Đối tượng"] = object_name
        if service:
            predicates.append("service_name = :service_name")
            params["service_name"] = service
            filters["Dịch vụ"] = service

        return self._query(days, predicates, params, filters, limit)

    def for_object(
        self,
        target: Target,
        days: int = DEFAULT_WINDOW_DAYS,
        *,
        limit: int = DEFAULT_ROW_LIMIT,
    ) -> AuditPage:
        """Audit events that name this object in their request parameters."""
        self.authz.require(Action.READ_AUDIT, target)
        self.authz.require_scope(target.catalog)
        return self.recent(
            days,
            object_name=target.full_name,
            limit=limit,
        )

    def unity_catalog_actions(
        self,
        days: int = DEFAULT_WINDOW_DAYS,
        *,
        limit: int = DEFAULT_ROW_LIMIT,
    ) -> AuditPage:
        """Unity Catalog events only, across the whole account.

        No workspace filter is applied and none can be: these rows carry
        ``workspace_id = 0`` at the top level, so adding ``workspace_id = <id>``
        would return an empty result that looks like an absence of events. The
        originating workspace is surfaced per row from
        ``request_params.workspace_id`` instead.
        """
        self.authz.require(Action.READ_AUDIT, None)
        page = self._query(
            days,
            ["service_name = :service_name"],
            {"service_name": UC_SERVICE_NAME},
            {"Dịch vụ": UC_SERVICE_NAME, "Phạm vi": "Toàn account (không lọc workspace)"},
            limit,
        )
        page.caveats.insert(0, WORKSPACE_NOTE)
        return page

    def _query(
        self,
        days: int,
        predicates: list[str],
        params: dict[str, str],
        filters: dict[str, str],
        limit: int,
    ) -> AuditPage:
        start, end, window_note = self._window(days)
        page = AuditPage(
            observed_at=now_text(),
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            filters=dict(filters),
            caveats=self._standard_caveats(window_note, filters),
        )

        where = " AND ".join([_WINDOW] + predicates)
        statement = (
            f"SELECT{_COLUMNS}FROM {AUDIT_TABLE}\nWHERE {where}\nORDER BY event_time DESC"
        )
        bound = dict(params)
        bound["since"] = start.isoformat()
        bound["until"] = end.isoformat()
        row_limit = max(1, min(int(limit or DEFAULT_ROW_LIMIT), SQL_MAX_ROWS))

        def run() -> Listing:
            result = self.runner.run(statement, bound, row_limit=row_limit)
            page.rows = [self._row(raw) for raw in result.rows]
            page.truncated = result.truncated
            completeness = (
                Completeness.TRUNCATED if result.truncated else Completeness.COMPLETE
            )
            return Listing(list(page.rows), completeness, page.observed_at)

        # self.listing keeps a failure inside the result: an audit panel that
        # cannot load should explain itself, not blank the screen or leak an SDK
        # exception.
        listing = self.listing(run)
        page.completeness = listing.completeness
        page.error = listing.error
        if page.truncated:
            page.caveats.append(
                f"Kết quả đã bị cắt ở {row_limit} dòng gần nhất. Hãy thu hẹp cửa sổ thời gian "
                "hoặc thêm bộ lọc để thấy phần còn lại."
            )
        if page.error is not None:
            page.rows = []
        return page

    @staticmethod
    def _row(raw: dict) -> AuditRow:
        def text(key: str) -> str:
            value = raw.get(key)
            return "" if value is None else str(value)

        return AuditRow(
            event_time=text("event_time"),
            event_date=text("event_date"),
            service_name=text("service_name"),
            action_name=text("action_name"),
            initiator=text("initiator"),
            run_as=text("run_as"),
            source_workspace_id=text("source_workspace_id"),
            record_workspace_id=text("record_workspace_id"),
            object_name=text("object_name"),
            securable_type=text("securable_type"),
            status_code=text("status_code"),
            audit_level=text("audit_level"),
            event_id=text("event_id"),
            source_ip=text("source_ip"),
        )

    @staticmethod
    def _window(days: int) -> tuple[date, date, str]:
        """Clamp the look-back and return the bound partition range.

        Every statement binds this range: ``event_date`` is the partition column
        and Databricks refuses a query over the audit table without a selective
        predicate on it.
        """
        try:
            requested = int(days)
        except (TypeError, ValueError):
            raise GovernanceError(
                Code.INVALID_INPUT, "Số ngày phải là một số nguyên."
            ) from None
        if requested < 1:
            raise GovernanceError(Code.INVALID_INPUT, "Cửa sổ thời gian tối thiểu là 1 ngày.")

        note = ""
        if requested > MAX_WINDOW_DAYS:
            note = (
                f"Đã thu hẹp cửa sổ từ {requested} xuống {MAX_WINDOW_DAYS} ngày: "
                "mỗi truy vấn trên bảng kiểm toán bị giới hạn để không quét quá nhiều phân vùng."
            )
            requested = MAX_WINDOW_DAYS

        # event_date is a UTC date, so the window is computed in UTC. Using the
        # server's local date would shift the partition range by a day.
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=requested - 1)
        return start, end, note

    @staticmethod
    def _standard_caveats(window_note: str, filters: dict) -> list[str]:
        caveats = [AUDIT_CAVEAT, TIMEZONE_NOTE, IDENTITY_NOTE, REDACTION_NOTE,
                   RETENTION_NOTE, SCHEMA_DRIFT_NOTE, PREVIEW_NOTE]
        if window_note:
            caveats.insert(0, window_note)
        if "Đối tượng" in filters:
            caveats.insert(1, OBJECT_MATCH_NOTE)
        return caveats

    # -- honest limits ----------------------------------------------------
    @staticmethod
    def omitted_request_params() -> dict:
        """The request_params Databricks drops without telling the caller."""
        return {"keys": REDACTED_REQUEST_PARAMS, "note": REDACTION_NOTE}

    @staticmethod
    def correlation_note() -> str:
        return CORRELATION_NOTE

    def correlate(self, event, page: AuditPage) -> list[dict]:
        """Best-effort match between one app operation and audit rows.

        Databricks never echoes this app's ``operation_id``/``event_id`` back, so
        there is no key to join on. What comes back here is a *candidate* list
        matched on (time window, actor, object, action-ish), and every row says
        so. It is never evidence that a specific audit record is this operation.
        """
        actor = (getattr(event, "actor", "") or "").strip().lower()
        run_as = (getattr(event, "execution_identity", "") or "").strip().lower()
        target = (getattr(event, "target", "") or "").strip().lower()
        stamp = (getattr(event, "time_utc", "") or "")[:16]

        out: list[dict] = []
        for row in page.rows:
            identities = {row.initiator.lower(), row.run_as.lower()} - {""}
            actor_hit = bool(identities & ({actor, run_as} - {""}))
            object_hit = bool(target) and row.object_name.lower() == target
            if not (actor_hit or object_hit):
                continue
            candidate = row.row()
            candidate["Mức khớp"] = (
                "Khớp cả danh tính và đối tượng" if actor_hit and object_hit
                else ("Chỉ khớp danh tính" if actor_hit else "Chỉ khớp đối tượng")
            )
            candidate["Đối chiếu"] = "GẦN ĐÚNG — không phải phép nối chính xác"
            candidate["Thao tác ứng dụng lúc"] = stamp or "—"
            out.append(candidate)
        return out

    # -- governance review ------------------------------------------------
    def review(self, checks_input: ReviewInput) -> ReviewReport:
        """Run the rule-based checks over data other services already read.

        Nothing here queries Databricks: a check may only report on data the app
        genuinely had. Each result is a finding - a signal to review - and no
        finding ever triggers a permission change.
        """
        self.authz.require(Action.READ_AUDIT, None)
        if not isinstance(checks_input, ReviewInput):
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Dữ liệu đầu vào cho rà soát phải là ReviewInput do các màn hình khác cung cấp.",
            )

        observed_at = now_text()
        scope = checks_input.scope_note or self.settings.scope_label
        base_caveat = (
            f"Chỉ rà soát dữ liệu ứng dụng đọc được trong phạm vi: {scope}. "
            f"Mức đầy đủ của dữ liệu nguồn: "
            f"{Completeness.LABELS.get(checks_input.completeness, checks_input.completeness)}."
        )

        assets = list(checks_input.assets or ())
        grants = list(checks_input.grants or ())
        shares = list(checks_input.external_shares or ())

        findings = [
            self._check_missing_owner(assets, checks_input, observed_at, base_caveat),
            self._check_missing_description(assets, checks_input, observed_at, base_caveat),
            self._check_broad_principals(grants, checks_input, observed_at, base_caveat),
            self._check_broad_privileges(grants, checks_input, observed_at, base_caveat),
            self._check_external_sharing(shares, checks_input, observed_at, base_caveat),
        ]
        return ReviewReport(
            findings=findings,
            observed_at=observed_at,
            completeness=checks_input.completeness,
            scope_note=scope,
        )

    def _check_missing_owner(self, assets, data, observed_at, caveat) -> Finding:
        rows = []
        for asset in assets:
            fields = _fields(asset)
            if not (fields.get("owner") or "").strip():
                rows.append({
                    "Đối tượng": fields.get("full_name", ""),
                    "Loại": fields.get("type_label", ""),
                    "Quan sát được": "Metadata không có chủ sở hữu",
                })
        return self._finding(
            RULE_MISSING_OWNER, rows, data.asset_source, observed_at, data.completeness,
            caveat + " Một số đối tượng có thể có chủ sở hữu mà danh tính thực thi không đọc được.",
            len(assets),
        )

    def _check_missing_description(self, assets, data, observed_at, caveat) -> Finding:
        rows = []
        for asset in assets:
            fields = _fields(asset)
            if not (fields.get("comment") or "").strip():
                rows.append({
                    "Đối tượng": fields.get("full_name", ""),
                    "Loại": fields.get("type_label", ""),
                    "Quan sát được": "Chưa có mô tả (comment)",
                })
        return self._finding(
            RULE_MISSING_DESCRIPTION, rows, data.asset_source, observed_at,
            data.completeness, caveat, len(assets),
        )

    def _check_broad_principals(self, grants, data, observed_at, caveat) -> Finding:
        rows = []
        for grant in grants:
            fields = _fields(grant)
            principal = (fields.get("principal") or "").strip()
            if principal.lower() in BROAD_PRINCIPALS:
                rows.append({
                    "Đối tượng": fields.get("object", ""),
                    "Principal": principal,
                    "Quyền": fields.get("privilege", ""),
                    "Nguồn quyền": fields.get("source", ""),
                    "Quan sát được": "Principal bao trùm gần như toàn bộ account",
                })
        return self._finding(
            RULE_BROAD_PRINCIPAL, rows, data.grant_source, observed_at, data.completeness,
            caveat + " Nhóm tuỳ biến cũng có thể rất rộng; danh sách này chỉ nhận diện "
            "các nhóm dựng sẵn có tên cố định.",
            len(grants),
        )

    def _check_broad_privileges(self, grants, data, observed_at, caveat) -> Finding:
        rows = []
        for grant in grants:
            fields = _fields(grant)
            code = (fields.get("privilege") or "").strip().upper()
            if code in priv.BROAD:
                rows.append({
                    "Đối tượng": fields.get("object", ""),
                    "Principal": fields.get("principal", ""),
                    "Quyền": priv.display(code),
                    "Mã Databricks": code,
                    "Nguồn quyền": fields.get("source", ""),
                    "Quan sát được": "Quyền mở rộng phạm vi vượt xa chính đối tượng",
                })
        return self._finding(
            RULE_BROAD_PRIVILEGE, rows, data.grant_source, observed_at, data.completeness,
            caveat + " Quyền kế thừa từ catalog/schema chỉ xuất hiện ở đây nếu màn hình gọi "
            "đã truyền vào dữ liệu quyền hiệu lực.",
            len(grants),
        )

    def _check_external_sharing(self, shares, data, observed_at, caveat) -> Finding:
        rows = []
        for entry in shares:
            fields = _fields(entry)
            rows.append({
                "Đối tượng": fields.get("object", ""),
                "Share": fields.get("share", ""),
                "Recipient": fields.get("recipient", ""),
                "Chi tiết": fields.get("detail", ""),
                "Quan sát được": "Đang nằm trong một share ra bên ngoài",
            })
        return self._finding(
            RULE_EXTERNAL_SHARING, rows, data.sharing_source, observed_at, data.completeness,
            caveat + " Chỉ phản ánh dữ liệu chia sẻ mà màn hình Chia sẻ dữ liệu truyền vào; "
            "ứng dụng không tự quét toàn bộ share.",
            len(shares),
        )

    @staticmethod
    def _finding(rule_id, rows, source, observed_at, completeness, caveat, examined) -> Finding:
        title, severity, _ = RULES[rule_id]
        return Finding(
            rule_id=rule_id,
            title=title,
            severity=severity,
            rows=rows,
            source=source,
            observed_at=observed_at,
            completeness=completeness,
            caveat=caveat,
            examined=examined,
        )


def _fields(item: Any) -> dict:
    """Read the fields a check needs from a dataclass, an object or a dict.

    Callers hand in whatever their own service produced - AssetRef, GrantRow
    plus an object name, or a plain dict - and no check should break because one
    screen passes dicts and another passes dataclasses.
    """
    def get(key: str):
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    out = {}
    for key in ("full_name", "owner", "comment", "object", "principal",
                "privilege", "source", "share", "recipient", "detail"):
        value = get(key)
        out[key] = "" if value is None else str(value)

    # AssetRef exposes the human label as a property, not a field.
    label = get("type_label")
    out["type_label"] = "" if label is None else str(label)
    if not out["object"]:
        out["object"] = out["full_name"]
    if not out["full_name"]:
        out["full_name"] = out["object"]
    return out


def describe_requirements() -> list[dict]:
    """What an administrator must arrange before this module can read anything."""
    return [{"Yêu cầu": text} for text in REQUIREMENTS]


def distinction_note() -> str:
    """Why this module and :mod:`ucg.audit` are not the same thing."""
    return (
        "Nhật ký kiểm toán Databricks (`system.access.audit`) là bản ghi chính thức, do "
        "Databricks ghi và lưu 365 ngày. Nhật ký thao tác của ứng dụng chỉ ghi những gì ứng "
        "dụng này làm trong phiên hiện tại và mất đi khi compute dừng. Khi hai bên khác nhau, "
        "bản ghi của Databricks là bản ghi có hiệu lực."
    )
