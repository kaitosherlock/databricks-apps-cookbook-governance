"""Lakehouse Federation connections, read without reading their secrets.

A federation connection is the one Unity Catalog securable that *contains*
credentials rather than pointing at them. ``ConnectionInfo.options`` is a plain
``Dict[str, str]`` holding whatever the connector needs to authenticate: host,
port, user, ``password``, ``personal_access_token``, ``client_secret``,
``private_key``. Databricks does not document whether a GET redacts a plaintext
value, so this module assumes it does **not**.

The rule that follows from that, and that every function here obeys:

    THE OPTIONS MAP NEVER LEAVES THIS MODULE.

It is never returned, never rendered, never logged, never fingerprinted, never
put in a plan payload and never written to the audit trail. Callers get
:class:`OptionSummary` rows instead: the key, a verdict about the key, and a
value only when the key is on :data:`SAFE_OPTION_KEYS` and its name carries no
hint of a secret. The one place a raw options map is touched is inside the
owner-update executor, which re-reads it and hands it straight back to
``w.connections.update`` without it ever being stored.

The second consequence is a governance limit worth stating plainly rather than
papering over: because the API will not show a credential, this app cannot
answer "has this connection's password been rotated?". See
:data:`SECRET_ROTATION_LIMIT`.

Scope of writes: read plus owner transfer. Creating a connection means
accepting a host, a port and a credential from whoever is on the page, and this
app refuses to be that door - see :data:`CREATION_IS_CONTROLLED`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..authz import Action
from ..errors import Code, GovernanceError
from ..naming import Target, validate_component
from ..paging import Completeness, take
from ..plans import Outcome, Plan, PreviewLine, fingerprint
from .base import Listing, Service, as_dict, enum_value, millis_to_text, now_text

#: How many objects one screen pulls before it admits it stopped early.
PAGE_LIMIT = 300

#: ``CatalogInfo.catalog_type`` for a catalog backed by a federation connection.
FOREIGN_CATALOG_TYPE = "FOREIGN_CATALOG"


# -- what counts as a secret -------------------------------------------------

#: Substrings that make an option key sensitive. Matched case-insensitively
#: anywhere in the key, because connectors spell the same idea a dozen ways
#: (``password``, ``pwd``… ``client_secret``, ``clientSecret``,
#: ``PersonalAccessToken``). A false positive costs a hidden value; a false
#: negative costs a leaked credential, so this list errs wide.
SENSITIVE_KEY_HINTS: tuple[str, ...] = (
    "password", "passwd", "pwd", "secret", "token", "key", "credential",
    "private", "signature", "sas", "auth", "session", "passphrase",
)

#: Keys whose value may be shown. Everything not listed here is reported as
#: present-but-hidden even when it is not obviously a credential: an allowlist
#: is the only form of redaction that stays correct when a new connector adds a
#: new option name.
SAFE_OPTION_KEYS: frozenset[str] = frozenset({
    "host", "hostname", "server", "instance", "instance_url", "port",
    "database", "dbname", "db", "catalog", "schema", "warehouse", "sfwarehouse",
    "project_id", "projectid", "dataset", "account", "region", "role",
    "service_name", "sid", "http_path", "httppath", "environment", "version",
    "ssl", "sslmode", "encrypt", "trustservercertificate", "connection_type",
    "auth_type", "authtype", "endpoint", "workspace", "site", "tenant",
})

#: Databricks recommends option values be secret references rather than
#: literals: ``secret('<scope>','<key>')``. The scope and key *names* are not
#: credentials, so a reference is safe to display - and displaying it is the
#: point, since it is the only thing that makes the credential auditable.
_SECRET_REFERENCE = re.compile(
    r"""^\s*secret\s*\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)\s*$""",
    re.IGNORECASE,
)

#: Values that look like Databricks already blanked them out. If any option
#: comes back looking like this, the round-trip that ``update`` requires is not
#: safe - see :meth:`FederationService.plan_update_owner`.
_REDACTION_MARKERS: tuple[str, ...] = (
    "redacted", "***", "[hidden]", "<hidden>", "********", "xxxxxxxx",
)


class OptionStatus:
    """What we are willing to say about one option key."""

    VISIBLE = "visible"
    SECRET_REFERENCE = "secret_reference"
    HIDDEN_SENSITIVE = "hidden_sensitive"
    HIDDEN_UNLISTED = "hidden_unlisted"
    SUSPECT_REDACTED = "suspect_redacted"

    LABELS = {
        VISIBLE: "Hiển thị (không nhạy cảm)",
        SECRET_REFERENCE: "Tham chiếu secret scope",
        HIDDEN_SENSITIVE: "Có giá trị — đã ẩn (tên khoá gợi ý bí mật)",
        HIDDEN_UNLISTED: "Có giá trị — đã ẩn (không nằm trong danh sách cho phép hiển thị)",
        SUSPECT_REDACTED: "Giá trị trả về trông như đã bị Databricks che",
    }


# -- the honest limits, as text the UI can print verbatim --------------------

SECRET_ROTATION_LIMIT = (
    "Ứng dụng KHÔNG thể kiểm tra credential của kết nối đã được xoay vòng hay chưa. "
    "Unity Catalog không công bố việc API có che giá trị bí mật trong options hay không, "
    "nên ứng dụng cố tình không đọc và không hiển thị chúng. "
    "Việc quản trị credential phải thực hiện ở nơi chứa nó: secret scope của Databricks "
    "hoặc kho bí mật của hệ thống nguồn, và được kiểm toán riêng ở đó."
)

CREATION_IS_CONTROLLED = (
    "Ứng dụng không tạo kết nối federation. Tạo kết nối đồng nghĩa với việc nhận host, "
    "cổng và credential từ người đang mở trang, rồi để danh tính thực thi của ứng dụng "
    "gửi đi — đó là một đường dẫn dữ liệu ra ngoài mà ứng dụng quản trị không nên mở. "
    "Hãy tạo kết nối trong Databricks (Catalog Explorer hoặc CREATE CONNECTION) "
    "theo quy trình có kiểm soát, rồi quay lại đây để soát xét."
)

NO_WORKSPACE_BINDING = (
    "Kết nối KHÔNG gán được theo workspace. “connection” không phải securable_type "
    "được tài liệu hoá cho API workspace bindings, nên ứng dụng không cung cấp chức năng này. "
    "Muốn giới hạn phạm vi, hãy ràng buộc foreign catalog dựng trên kết nối đó."
)

FOREIGN_TABLE_READ_ONLY = (
    "Bảng foreign qua Lakehouse Federation là CHỈ ĐỌC. Chúng vẫn là securable loại TABLE "
    "nên giao diện quyền có thể liệt kê MODIFY, nhưng cấp MODIFY trên bảng foreign "
    "không mang lại khả năng ghi nào."
)

CREATE_FOREIGN_CATALOG_REQUIREMENT = (
    "Tạo foreign catalog cần HAI quyền trên HAI securable khác nhau: "
    "CREATE CATALOG trên metastore, VÀ quyền sở hữu kết nối hoặc "
    "CREATE FOREIGN CATALOG trên chính kết nối đó. "
    "Có quyền này mà thiếu quyền kia thì thao tác vẫn thất bại."
)

METADATA_IS_NOT_SOURCE_POLICY = (
    "Metadata nhìn thấy trong Unity Catalog KHÔNG phải là quyền và chính sách của hệ thống nguồn. "
    "Quyền trong Unity Catalog quyết định ai truy cập được qua Databricks; hệ thống nguồn vẫn "
    "áp dụng quyền, row-level security và masking của riêng nó lên tài khoản mà kết nối dùng. "
    "Hai lớp này phải được soát xét riêng."
)

COMPUTE_REQUIREMENT = (
    "Lakehouse Federation yêu cầu compute tối thiểu: SQL warehouse loại pro hoặc serverless "
    "từ phiên bản 2023.40 trở lên, hoặc cluster dùng Databricks Runtime 13.3 LTS trở lên. "
    "Compute thấp hơn sẽ lỗi khi truy vấn bảng foreign, không phải lỗi cấu hình kết nối."
)

CONNECTOR_SUPPORT_CAVEAT = (
    "Khả năng hỗ trợ là THEO TỪNG CONNECTOR, không đồng nhất. Databricks không công bố "
    "một bảng trạng thái duy nhất cho toàn bộ connector, nên ứng dụng không khẳng định "
    "connector nào hỗ trợ đầy đủ. Hãy đối chiếu tài liệu của đúng loại kết nối trước khi "
    "dựa vào một tính năng cụ thể."
)

COMMENT_NOT_EDITABLE = (
    "Phiên bản SDK đang dùng không cho sửa mô tả kết nối: "
    "connections.update chỉ nhận options, new_name và owner. "
    "Sửa mô tả trong Databricks bằng ALTER CONNECTION … COMMENT."
)

#: Connection types Databricks documents as Beta. These are not all present in
#: the 0.105.0 ``ConnectionType`` enum, so the check is done on the wire string
#: a workspace actually returns rather than on the enum members.
BETA_CONNECTION_TYPES: frozenset[str] = frozenset({
    "GITHUB", "AWS_SECRETS_MANAGER", "AZURE_KEY_VAULT",
})

#: Per-connector notes worth showing next to a connection. Absence from this
#: map is not a verdict - it means we have nothing published to quote, which is
#: what :meth:`FederationService.connector_caveat` says out loud.
CONNECTOR_NOTES: dict[str, str] = {
    "MYSQL": "Nguồn quan hệ. Kiểm tra bộ ký tự và cách ánh xạ kiểu dữ liệu sang Unity Catalog.",
    "POSTGRESQL": "Nguồn quan hệ. Tên schema và bảng phân biệt hoa thường ở nguồn.",
    "SQLSERVER": "Nguồn quan hệ. Chú ý cấu hình mã hoá kết nối (encrypt / trust certificate).",
    "SQLDW": "Azure Synapse. Hiệu năng phụ thuộc cấu hình phía Synapse.",
    "REDSHIFT": "Yêu cầu đường mạng tới cluster Redshift; kiểm tra security group.",
    "SNOWFLAKE": "Xác thực có thể dùng mật khẩu, OAuth hoặc key-pair — mỗi kiểu đặt option khác nhau.",
    "BIGQUERY": "Dùng service account của Google; credential nằm trong options của kết nối.",
    "ORACLE": "Kiểm tra service name / SID và phiên bản driver được hỗ trợ.",
    "TERADATA": "Đối chiếu tài liệu về phiên bản Teradata được hỗ trợ.",
    "GLUE": "Kết nối tới AWS Glue Data Catalog, không phải tới dữ liệu quan hệ.",
    "HIVE_METASTORE": "Dùng cho federation metastore Hive; khác với các nguồn quan hệ.",
    "DATABRICKS": "Kết nối tới workspace Databricks khác; quyền vẫn do metastore đích quyết định.",
    "SALESFORCE": "Nguồn SaaS: giới hạn API và hạn mức truy vấn của Salesforce vẫn áp dụng.",
    "SALESFORCE_DATA_CLOUD": "Nguồn SaaS: giới hạn API của Salesforce Data Cloud vẫn áp dụng.",
    "SERVICENOW": "Nguồn SaaS: giới hạn API của ServiceNow vẫn áp dụng.",
    "WORKDAY_RAAS": "Đọc qua báo cáo RaaS của Workday; phạm vi dữ liệu do báo cáo quyết định.",
    "POWER_BI": "Kết nối phục vụ tích hợp Power BI, không phải nguồn dữ liệu quan hệ.",
    "GA4_RAW_DATA": "Dữ liệu thô Google Analytics 4; cấu trúc do GA4 export quyết định.",
    "HTTP": "Kết nối HTTP dùng cho tích hợp dịch vụ ngoài; kiểm tra kỹ endpoint được cấu hình.",
    "UNKNOWN_CONNECTION_TYPE": "SDK không nhận diện được loại kết nối này.",
}


# -- value objects -----------------------------------------------------------

@dataclass
class OptionSummary:
    """One option key, described without disclosing a credential."""

    key: str
    status: str
    #: Only ever set when ``status`` is VISIBLE or SECRET_REFERENCE.
    value: str = ""

    @property
    def status_label(self) -> str:
        return OptionStatus.LABELS.get(self.status, self.status)

    @property
    def is_secret_reference(self) -> bool:
        return self.status == OptionStatus.SECRET_REFERENCE

    @property
    def is_hidden(self) -> bool:
        return self.status in (
            OptionStatus.HIDDEN_SENSITIVE,
            OptionStatus.HIDDEN_UNLISTED,
            OptionStatus.SUSPECT_REDACTED,
        )

    def row(self) -> dict:
        return {
            "Tham số": self.key,
            "Trạng thái": self.status_label,
            "Giá trị": self.value if self.value else "— (đã ẩn)",
        }


@dataclass
class Finding:
    """One soát-xét observation. Never carries an option value."""

    connection: str
    title: str
    detail: str
    #: positive | review | unknown | info
    kind: str = "review"

    KIND_LABELS = {
        "positive": "Đạt",
        "review": "Cần soát xét",
        "unknown": "Chưa xác định",
        "info": "Thông tin",
    }

    def row(self) -> dict:
        return {
            "Kết nối": self.connection,
            "Mức": self.KIND_LABELS.get(self.kind, self.kind),
            "Phát hiện": self.title,
            "Chi tiết": self.detail,
        }


@dataclass
class ConnectionRef:
    """A connection as a picker needs it: identity and lifecycle, no options."""

    name: str
    connection_type: str = ""
    credential_type: str = ""
    owner: str = ""
    comment: str = ""
    read_only: bool = False
    provisioning_state: str = ""
    created: str = ""
    created_by: str = ""
    updated: str = ""
    updated_by: str = ""

    @property
    def target(self) -> Target:
        return Target("connection", self.name)

    @property
    def is_beta(self) -> bool:
        return self.connection_type in BETA_CONNECTION_TYPES

    def row(self) -> dict:
        return {
            "Kết nối": self.name,
            "Loại": self.connection_type or "Chưa xác định",
            "Chủ sở hữu": self.owner or "Chưa xác định",
            "Chỉ đọc": "Có" if self.read_only else "Không",
            "Trạng thái cấp phát": self.provisioning_state or "Chưa xác định",
            "Cập nhật lần cuối": self.updated or "—",
        }


@dataclass
class ConnectionDetail:
    """One connection, already redacted. Safe to hand to any renderer."""

    ref: ConnectionRef
    options: list[OptionSummary] = field(default_factory=list)
    #: Present so the UI can say "13 tham số" without listing values.
    option_count: int = 0
    findings: list[Finding] = field(default_factory=list)
    observed_at: str = ""

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def secret_reference_keys(self) -> list[str]:
        return [o.key for o in self.options if o.is_secret_reference]

    @property
    def literal_secret_keys(self) -> list[str]:
        """Sensitive keys whose value is a literal, not a secret() reference."""
        return [o.key for o in self.options if o.status == OptionStatus.HIDDEN_SENSITIVE]

    @property
    def suspect_redacted_keys(self) -> list[str]:
        return [o.key for o in self.options if o.status == OptionStatus.SUSPECT_REDACTED]

    def option_rows(self) -> list[dict]:
        return [o.row() for o in self.options]

    def notes(self) -> list[str]:
        return [
            SECRET_ROTATION_LIMIT,
            METADATA_IS_NOT_SOURCE_POLICY,
            NO_WORKSPACE_BINDING,
            COMPUTE_REQUIREMENT,
        ]


@dataclass
class ForeignCatalogRef:
    """A catalog whose data lives behind one connection."""

    name: str
    connection_name: str
    owner: str = ""
    comment: str = ""
    in_app_scope: bool = True

    def row(self) -> dict:
        return {
            "Catalog": self.name,
            "Kết nối": self.connection_name,
            "Chủ sở hữu": self.owner or "Chưa xác định",
            "Trong phạm vi ứng dụng": "Có" if self.in_app_scope else "Không",
        }


@dataclass
class DependencyReport:
    """What breaks if this connection changes - as far as we could see."""

    connection: str
    catalogs: list[ForeignCatalogRef] = field(default_factory=list)
    completeness: str = Completeness.COMPLETE
    observed_at: str = ""
    #: Set when the catalog listing itself failed.
    error: GovernanceError | None = None

    @property
    def exhaustive(self) -> bool:
        """Can this list be presented as the complete dependency set?"""
        return self.error is None and self.completeness == Completeness.COMPLETE

    @property
    def count_text(self) -> str:
        if not self.exhaustive:
            return "Chưa xác định"
        return str(len(self.catalogs))

    def verdict(self) -> str:
        if self.error is not None:
            return (
                "Chưa xác định — không đọc được danh sách catalog, nên KHÔNG thể kết luận "
                "kết nối này có đang được dùng hay không."
            )
        if self.completeness == Completeness.TRUNCATED:
            return (
                "Chưa xác định — danh sách catalog đã bị cắt bớt vì quá dài. "
                "Những catalog liệt kê bên dưới là có thật, nhưng có thể còn catalog khác."
            )
        if self.completeness == Completeness.PARTIAL_PERMISSION:
            return (
                "Chưa xác định — danh tính thực thi chỉ thấy được một phần catalog. "
                "Danh sách phụ thuộc bên dưới KHÔNG phải là đầy đủ."
            )
        if not self.catalogs:
            return (
                "Không thấy foreign catalog nào trỏ tới kết nối này trong phạm vi quyền "
                "của danh tính thực thi. Đây là “không có dữ liệu khớp”, không phải "
                "bảo đảm rằng không nơi nào đang dùng."
            )
        return (
            f"Có {len(self.catalogs)} foreign catalog đang dựa trên kết nối này. "
            "Xoá hoặc đổi kết nối sẽ làm chúng hỏng."
        )

    def rows(self) -> list[dict]:
        return [c.row() for c in self.catalogs]


# -- redaction ---------------------------------------------------------------

def is_sensitive_key(key: str) -> bool:
    """Does the key name suggest it holds a credential?"""
    lowered = (key or "").lower()
    return any(hint in lowered for hint in SENSITIVE_KEY_HINTS)


def secret_reference(value: str) -> tuple[str, str] | None:
    """Parse ``secret('scope','key')`` into (scope, key), or None."""
    match = _SECRET_REFERENCE.match(value or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def _looks_redacted(value: str) -> bool:
    lowered = (value or "").strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in _REDACTION_MARKERS)


def summarize_options(options: dict | None) -> list[OptionSummary]:
    """Turn a raw options map into rows that carry no credential.

    This is the only transformation applied to ``ConnectionInfo.options``, and
    its output is the only thing any caller ever sees.
    """
    rows: list[OptionSummary] = []
    for key in sorted((options or {}).keys(), key=str.lower):
        value = str((options or {}).get(key) or "")
        reference = secret_reference(value)
        if reference is not None:
            scope, secret_key = reference
            rows.append(OptionSummary(
                key=key,
                status=OptionStatus.SECRET_REFERENCE,
                # Scope and key names identify where the credential lives; they
                # are not the credential and are what makes it auditable.
                value=f"secret('{scope}', '{secret_key}')",
            ))
            continue
        if _looks_redacted(value):
            rows.append(OptionSummary(key=key, status=OptionStatus.SUSPECT_REDACTED))
            continue
        if is_sensitive_key(key):
            rows.append(OptionSummary(key=key, status=OptionStatus.HIDDEN_SENSITIVE))
            continue
        if key.lower() in SAFE_OPTION_KEYS:
            rows.append(OptionSummary(key=key, status=OptionStatus.VISIBLE, value=value))
            continue
        rows.append(OptionSummary(key=key, status=OptionStatus.HIDDEN_UNLISTED))
    return rows


class FederationService(Service):
    """Read-and-soát-xét over Lakehouse Federation connections.

    Writes are limited to owner transfer on purpose; see
    :data:`CREATION_IS_CONTROLLED`.
    """

    capability_keys = ("federation.connections",)

    # -- constants re-exported so a screen imports one thing ---------------
    SECRET_ROTATION_LIMIT = SECRET_ROTATION_LIMIT
    CREATION_IS_CONTROLLED = CREATION_IS_CONTROLLED
    NO_WORKSPACE_BINDING = NO_WORKSPACE_BINDING
    FOREIGN_TABLE_READ_ONLY = FOREIGN_TABLE_READ_ONLY
    CREATE_FOREIGN_CATALOG_REQUIREMENT = CREATE_FOREIGN_CATALOG_REQUIREMENT
    METADATA_IS_NOT_SOURCE_POLICY = METADATA_IS_NOT_SOURCE_POLICY
    COMPUTE_REQUIREMENT = COMPUTE_REQUIREMENT
    CONNECTOR_SUPPORT_CAVEAT = CONNECTOR_SUPPORT_CAVEAT
    COMMENT_NOT_EDITABLE = COMMENT_NOT_EDITABLE

    # -- capability answers -----------------------------------------------
    @staticmethod
    def can_create_connection() -> tuple[bool, str]:
        return False, CREATION_IS_CONTROLLED

    @staticmethod
    def can_bind_to_workspaces() -> tuple[bool, str]:
        return False, NO_WORKSPACE_BINDING

    @staticmethod
    def can_update_comment() -> tuple[bool, str]:
        return False, COMMENT_NOT_EDITABLE

    @staticmethod
    def can_verify_credential_rotation() -> tuple[bool, str]:
        return False, SECRET_ROTATION_LIMIT

    @staticmethod
    def connector_caveat(connection_type: str) -> str:
        """What we are willing to say about one connector, and no more."""
        code = (connection_type or "").strip().upper()
        if not code:
            return (
                "Chưa xác định loại kết nối, nên không có lưu ý riêng cho connector. "
                + CONNECTOR_SUPPORT_CAVEAT
            )
        parts = [CONNECTOR_SUPPORT_CAVEAT]
        if code in BETA_CONNECTION_TYPES:
            parts.insert(0, f"Loại kết nối {code} đang ở mức Beta theo tài liệu Databricks.")
        note = CONNECTOR_NOTES.get(code)
        if note:
            parts.insert(0, f"{code}: {note}")
        else:
            parts.insert(
                0,
                f"{code}: ứng dụng không có ghi chú riêng cho connector này — "
                "chưa xác định, hãy tra tài liệu Databricks cho đúng loại kết nối.",
            )
        return " ".join(parts)

    # -- reading -----------------------------------------------------------
    def connections(self) -> Listing:
        """Every connection the executing identity can see."""
        self.authz.require(Action.READ_STORAGE, None)

        def run() -> Listing:
            items, more = take(self.w.connections.list(max_results=0), PAGE_LIMIT)
            refs = [self._ref(as_dict(item)) for item in items]
            refs.sort(key=lambda r: r.name.lower())
            return Listing(
                refs,
                Completeness.TRUNCATED if more else Completeness.COMPLETE,
                now_text(),
                note=(
                    "Danh sách chỉ gồm kết nối mà danh tính thực thi được phép nhìn thấy. "
                    "Ứng dụng không hiển thị options của bất kỳ kết nối nào."
                ),
            )

        return self.listing(run)

    def connection_detail(self, name: str) -> ConnectionDetail:
        """One connection, with options reduced to non-sensitive facts."""
        self.authz.require(Action.READ_STORAGE, None)
        name = validate_component(name, "Tên kết nối")
        raw = as_dict(self.read(lambda: self.w.connections.get(name=name)))
        ref = self._ref(raw)
        # ``raw`` still holds the live options map. It is consumed here and the
        # dict itself is never attached to the returned object.
        options = raw.get("options") or {}
        detail = ConnectionDetail(
            ref=ref,
            options=summarize_options(options),
            option_count=len(options),
            observed_at=now_text(),
        )
        detail.findings = self._findings_for(detail)
        return detail

    def _ref(self, data: dict) -> ConnectionRef:
        return ConnectionRef(
            name=data.get("name") or "",
            connection_type=enum_value(data.get("connection_type")).upper(),
            credential_type=enum_value(data.get("credential_type")).upper(),
            owner=data.get("owner") or "",
            comment=data.get("comment") or "",
            read_only=bool(data.get("read_only")),
            provisioning_state=enum_value(
                (as_dict(data.get("provisioning_info")) or {}).get("state")
            ).upper(),
            created=millis_to_text(data.get("created_at")),
            created_by=data.get("created_by") or "",
            updated=millis_to_text(data.get("updated_at")),
            updated_by=data.get("updated_by") or "",
        )

    # -- dependencies ------------------------------------------------------
    def foreign_catalogs(self, connection_name: str = "") -> Listing:
        """Foreign catalogs, optionally narrowed to one connection.

        The app's catalog scope filter is deliberately NOT applied here: hiding
        an out-of-scope catalog would understate what a connection change
        breaks. Out-of-scope catalogs are flagged instead of dropped.
        """
        self.authz.require(Action.READ_STORAGE, None)
        wanted = (connection_name or "").strip()
        if wanted:
            validate_component(wanted, "Tên kết nối")

        def run() -> Listing:
            items, more = take(self.w.catalogs.list(max_results=0), PAGE_LIMIT)
            refs: list[ForeignCatalogRef] = []
            for item in items:
                data = as_dict(item)
                if enum_value(data.get("catalog_type")).upper() != FOREIGN_CATALOG_TYPE:
                    continue
                linked = data.get("connection_name") or ""
                if wanted and linked != wanted:
                    continue
                catalog_name = data.get("name") or ""
                refs.append(ForeignCatalogRef(
                    name=catalog_name,
                    connection_name=linked,
                    owner=data.get("owner") or "",
                    comment=data.get("comment") or "",
                    in_app_scope=self.settings.in_scope(catalog_name),
                ))
            refs.sort(key=lambda r: r.name.lower())
            return Listing(
                refs,
                Completeness.TRUNCATED if more else Completeness.COMPLETE,
                now_text(),
                note=(
                    "Danh sách dựa trên các catalog mà danh tính thực thi nhìn thấy. "
                    "Catalog ngoài phạm vi ứng dụng vẫn được liệt kê vì chúng vẫn hỏng "
                    "nếu kết nối thay đổi."
                ),
            )

        return self.listing(run)

    def dependency_check(self, name: str) -> DependencyReport:
        """What a destructive change to this connection would take down."""
        name = validate_component(name, "Tên kết nối")
        listing = self.foreign_catalogs(name)
        return DependencyReport(
            connection=name,
            catalogs=list(listing.items),
            completeness=listing.completeness,
            observed_at=listing.observed_at or now_text(),
            error=listing.error,
        )

    # -- soát xét ----------------------------------------------------------
    def findings(self, limit: int = PAGE_LIMIT) -> Listing:
        """Credential-hygiene and lifecycle observations across connections.

        A per-connection GET is required because ``list`` and ``get`` both
        return options, but only a GET is a defensible place to read one
        connection's configuration. Nothing here reports a value.
        """
        self.authz.require(Action.READ_STORAGE, None)

        def run() -> Listing:
            listed = self.connections()
            if not listed.ok:
                raise listed.error
            rows: list[Finding] = []
            completeness = listed.completeness
            for ref in list(listed.items)[:limit]:
                try:
                    detail = self.connection_detail(ref.name)
                except GovernanceError as err:
                    # One unreadable connection must not blank the report, and
                    # must not be silently counted as clean.
                    completeness = Completeness.PARTIAL_PERMISSION
                    rows.append(Finding(
                        connection=ref.name,
                        title="Không đọc được cấu hình kết nối",
                        detail=(
                            f"{err.message} Chưa xác định tình trạng credential của kết nối này — "
                            "đây là thiếu quyền hoặc lỗi đọc, không phải kết luận “không có vấn đề”."
                        ),
                        kind="unknown",
                    ))
                    continue
                rows.extend(detail.findings)
            return Listing(
                rows, completeness, now_text(),
                note=(
                    "Soát xét chỉ dựa trên metadata kết nối. "
                    + METADATA_IS_NOT_SOURCE_POLICY
                ),
            )

        return self.listing(run)

    def _findings_for(self, detail: ConnectionDetail) -> list[Finding]:
        name = detail.name
        out: list[Finding] = []

        references = detail.secret_reference_keys
        if references:
            out.append(Finding(
                connection=name,
                title="Dùng tham chiếu secret scope",
                detail=(
                    "Các tham số sau trỏ tới secret scope thay vì ghi giá trị trực tiếp: "
                    + ", ".join(references)
                    + ". Đây là cách Databricks khuyến nghị: credential được xoay vòng "
                    "và kiểm toán tại secret scope."
                ),
                kind="positive",
            ))

        literals = detail.literal_secret_keys
        if literals:
            out.append(Finding(
                connection=name,
                title="Tham số nhạy cảm có vẻ ghi giá trị trực tiếp",
                detail=(
                    "Các tham số sau mang tên gợi ý bí mật nhưng giá trị không theo dạng "
                    f"secret('<scope>','<key>'): {', '.join(literals)}. "
                    "Cần soát xét và chuyển sang tham chiếu secret scope. "
                    "Ứng dụng không đọc và không hiển thị giá trị của chúng."
                ),
                kind="review",
            ))

        suspects = detail.suspect_redacted_keys
        if suspects:
            out.append(Finding(
                connection=name,
                title="Giá trị trả về trông như đã bị che",
                detail=(
                    f"Các tham số sau trả về giá trị dạng che: {', '.join(suspects)}. "
                    "Chưa xác định đây là giá trị thật hay do API che. "
                    "Vì vậy ứng dụng không thực hiện cập nhật nào phải gửi lại options."
                ),
                kind="unknown",
            ))

        if not detail.ref.owner:
            out.append(Finding(
                connection=name,
                title="Chưa xác định chủ sở hữu",
                detail=(
                    "API không trả về owner cho kết nối này. Thiếu chủ sở hữu rõ ràng thì "
                    "không có ai chịu trách nhiệm cấp CREATE FOREIGN CATALOG. "
                    + CREATE_FOREIGN_CATALOG_REQUIREMENT
                ),
                kind="unknown",
            ))

        state = detail.ref.provisioning_state
        if state and state not in ("ACTIVE", "STATE_UNSPECIFIED"):
            out.append(Finding(
                connection=name,
                title=f"Trạng thái cấp phát: {state}",
                detail="Kết nối chưa ở trạng thái hoạt động; truy vấn qua kết nối này có thể lỗi.",
                kind="review",
            ))

        if detail.ref.is_beta:
            out.append(Finding(
                connection=name,
                title=f"Connector {detail.ref.connection_type} ở mức Beta",
                detail=self.connector_caveat(detail.ref.connection_type),
                kind="review",
            ))

        out.append(Finding(
            connection=name,
            title="Không kiểm tra được vòng đời credential",
            detail=SECRET_ROTATION_LIMIT,
            kind="unknown",
        ))

        dependencies = self.dependency_check(name)
        out.append(Finding(
            connection=name,
            title=f"Foreign catalog phụ thuộc: {dependencies.count_text}",
            detail=dependencies.verdict(),
            kind="info" if dependencies.exhaustive and not dependencies.catalogs else "review",
        ))
        return out

    # -- planning ----------------------------------------------------------
    def plan_update_owner(self, name: str, new_owner: str, reason: str) -> Plan:
        """Validate -> Authorize -> Build plan -> Preview. Nothing is sent.

        ``connections.update`` takes ``options`` as a required argument, so an
        owner change is physically a full-options write. That is only safe when
        every value read back is a value we can hand straight back. If any value
        looks like Databricks blanked it, the update would overwrite a live
        credential with a placeholder, so the plan is refused instead.
        """
        target = Target("connection", name)
        self.authz.require(Action.MANAGE_STORAGE, target)

        new_owner = (new_owner or "").strip()
        reason = (reason or "").strip()
        if not new_owner or any(ord(c) < 32 for c in new_owner):
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Nhập principal nhận quyền sở hữu: email người dùng, tên account group, "
                "hoặc application ID của service principal.",
            )
        if len(new_owner) > 255:
            raise GovernanceError(Code.INVALID_INPUT, "Principal vượt quá 255 ký tự.")
        if not reason:
            raise GovernanceError(Code.INVALID_INPUT, "Nhập lý do thay đổi.")
        if len(reason) > 500:
            raise GovernanceError(Code.INVALID_INPUT, "Lý do tối đa 500 ký tự.")

        detail = self.connection_detail(target.full_name)
        if detail.ref.owner and detail.ref.owner.strip().lower() == new_owner.lower():
            raise GovernanceError(
                Code.ALREADY_SATISFIED,
                f"“{new_owner}” đã là chủ sở hữu của kết nối này.",
            )
        if detail.suspect_redacted_keys:
            raise GovernanceError(
                Code.CAPABILITY_UNAVAILABLE,
                "Không thể đổi chủ sở hữu qua ứng dụng: connections.update bắt buộc gửi lại "
                "toàn bộ options, mà một số giá trị đọc về trông như đã bị che "
                f"({', '.join(detail.suspect_redacted_keys)}). Gửi lại sẽ ghi đè credential thật "
                "bằng giá trị che. Hãy đổi chủ sở hữu trong Databricks bằng "
                "ALTER CONNECTION … OWNER TO.",
            )

        plan = Plan(
            action=Action.MANAGE_STORAGE,
            target_key=target.key,
            target_name=target.full_name,
            target_type=target.label,
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            # The payload is written to the audit trail, so it carries names
            # only: no options map, no option values, no key/value pairs.
            payload={
                "principal": new_owner,
                "operation": "update_owner",
                "connection": target.full_name,
                "previous_owner": detail.ref.owner or "",
            },
            before=self._fingerprint(detail),
            reason=reason,
            summary=f"Chuyển quyền sở hữu kết nối {target.full_name} cho {new_owner}",
        )
        plan.preview = self._preview(detail, new_owner)
        return plan

    def _preview(self, detail: ConnectionDetail, new_owner: str) -> list[PreviewLine]:
        ref = detail.ref
        lines = [
            PreviewLine(f"Đổi chủ sở hữu kết nối: {ref.name}", "change"),
            PreviewLine(
                f"Từ: {ref.owner or 'Chưa xác định'} → Sang: {new_owner}", "change"),
            PreviewLine(f"Loại kết nối: {ref.connection_type or 'Chưa xác định'}", "info"),
            PreviewLine(
                "Yêu cầu sẽ được gửi bằng: "
                + ("tài khoản dịch vụ của ứng dụng"
                   if self.ctx.execution_identity == "app_service_principal"
                   else "hồ sơ đăng nhập hiện tại"),
                "info",
            ),
            PreviewLine(
                f"Kết nối đang có {detail.option_count} tham số cấu hình. "
                "API bắt buộc gửi lại toàn bộ options khi đổi chủ sở hữu, nên ứng dụng "
                "sẽ đọc lại options ngay trước khi gửi và chuyển nguyên vẹn — "
                "không ghi ra nhật ký, không hiển thị, không lưu lại.",
                "warning",
            ),
            PreviewLine(
                "Chưa xác định được việc gửi lại options có giữ nguyên tuyệt đối mọi giá trị hay "
                "không: Databricks không công bố cách API xử lý giá trị bí mật khi đọc. "
                "Sau khi áp dụng, hãy kiểm tra một truy vấn qua kết nối này.",
                "unknown",
            ),
            PreviewLine(
                "Đổi chủ sở hữu KHÔNG đổi credential. " + SECRET_ROTATION_LIMIT,
                "info",
            ),
            PreviewLine(CREATE_FOREIGN_CATALOG_REQUIREMENT, "info"),
        ]
        if detail.literal_secret_keys:
            lines.append(PreviewLine(
                "Kết nối có tham số nhạy cảm ghi giá trị trực tiếp: "
                + ", ".join(detail.literal_secret_keys)
                + ". Nên chuyển sang secret('<scope>','<key>') trong Databricks.",
                "warning",
            ))

        dependencies = self.dependency_check(detail.name)
        if dependencies.catalogs:
            names = ", ".join(c.name for c in dependencies.catalogs[:20])
            lines.append(PreviewLine(
                f"Foreign catalog đang dựa trên kết nối này ({dependencies.count_text}): {names}"
                + (" …" if len(dependencies.catalogs) > 20 else ""),
                "warning",
            ))
        lines.append(PreviewLine(dependencies.verdict(),
                                 "info" if dependencies.exhaustive else "unknown"))
        lines.append(PreviewLine(NO_WORKSPACE_BINDING, "info"))
        lines.append(PreviewLine(METADATA_IS_NOT_SOURCE_POLICY, "info"))
        return lines

    @staticmethod
    def _fingerprint(detail: ConnectionDetail) -> str:
        """Hash the parts of the state a change here depends on.

        Option *values* are excluded on purpose: a hash of a secret is still a
        way to test guesses about it, and the key set plus owner is enough to
        detect that someone reconfigured the connection under us.
        """
        return fingerprint({
            "owner": detail.ref.owner,
            "connection_type": detail.ref.connection_type,
            "read_only": detail.ref.read_only,
            "option_keys": sorted(o.key for o in detail.options),
            "provisioning_state": detail.ref.provisioning_state,
        })

    # -- executing ---------------------------------------------------------
    def apply(self, plan: Plan, confirmation: str) -> Outcome:
        """Run one owner-transfer plan through the shared mutation pipeline."""
        plan.require_confirmation(confirmation)
        if plan.payload.get("operation") != "update_owner":
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Bản xem trước này không phải thao tác chuyển quyền sở hữu kết nối.",
            )
        name = str(plan.payload.get("connection") or "")
        target = Target("connection", name)
        new_owner = str(plan.payload.get("principal") or "")

        def revalidate() -> str:
            return self._fingerprint(self.connection_detail(name))

        def do():
            # Re-read immediately before writing: ``update`` requires the full
            # options map, and the only defensible source for it is Databricks
            # itself, one call earlier. The map lives in this frame and nowhere
            # else - it is not stored, returned, logged or fingerprinted.
            current = as_dict(self.w.connections.get(name=name))
            options = dict(current.get("options") or {})
            if any(_looks_redacted(str(v or "")) for v in options.values()):
                raise GovernanceError(
                    Code.CAPABILITY_UNAVAILABLE,
                    "Đã dừng trước khi gửi: options đọc về chứa giá trị trông như đã bị che, "
                    "gửi lại sẽ ghi đè credential thật. Đổi chủ sở hữu trong Databricks.",
                )
            self.w.connections.update(name=name, options=options, owner=new_owner)

        def verify() -> dict:
            after = self.connection_detail(name)
            return {
                "connection": name,
                "owner_now": after.ref.owner,
                "applied": after.ref.owner.strip().lower() == new_owner.strip().lower(),
                "option_count": after.option_count,
                "note": (
                    "Đã đọc lại chủ sở hữu và số lượng tham số. Giá trị của các tham số "
                    "không được đọc để so sánh, nên tính toàn vẹn của credential là "
                    "Chưa xác định — hãy chạy thử một truy vấn qua kết nối này."
                ),
            }

        return self.execute(
            plan, target, do,
            revalidate=revalidate, verify=verify,
            action_label=plan.summary,
        )
