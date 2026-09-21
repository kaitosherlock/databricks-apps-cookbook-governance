"""Reading and changing Unity Catalog tags, governed tags and data classification.

Tags look like the simplest thing in Unity Catalog and are not. The rules this
module exists to encode, each of which is a real bug when ignored:

* The Entity Tag Assignments API addresses objects by a **plural, lowercase
  entity type** - ``catalogs``, ``schemas``, ``tables``, ``columns``,
  ``volumes`` - and nothing else. Views are reached through ``tables``.
  Functions, registered models and model versions are taggable in the
  Databricks UI but **not** through this API, so they are refused with an
  explanation rather than silently returning an empty tag list.
* A column has no separate parameter: the entity type is ``columns`` and the
  entity name is the **four-part** ``catalog.schema.table.column``.
* ``entity_name``, ``entity_type`` and ``tag_key`` are **immutable**.
  ``update`` changes only ``tag_value`` and requires an ``update_mask``;
  renaming a key means delete then create.
* Assigning a tag needs ``APPLY_TAG`` on the object **plus** ``USE SCHEMA`` on
  the parent schema **plus** ``USE CATALOG`` on the parent catalog. A missing
  ``USE CATALOG`` is the usual cause of a 403 that looks like a bug, so the
  error path says so instead of repeating "permission denied".
* A **governed** tag needs a second, separate permission (``ASSIGN`` or
  ``MANAGE`` on the tag policy), granted through the Account Access Control
  Proxy API - not through a Unity Catalog ``GRANT``. There is no permissions
  method on ``w.tag_policies``, so this app cannot read or change it, and says
  that plainly.
* Tags do **not** inherit onto children, and reverse lookup ("who carries tag
  X") does not exist in this API at all. Both are exposed as constants the UI
  shows, because an empty panel is otherwise read as "nothing is tagged".

Vietnamese is the language of everything a user reads. Databricks object names,
privilege codes, tag keys and API names stay exactly as Databricks spells them.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from ..authz import Action
from ..errors import Code, GovernanceError, translate
from ..naming import Target, validate_component
from ..paging import Completeness, take
from ..plans import Plan, PreviewLine, fingerprint
from .base import Listing, Service, as_dict, enum_value, millis_to_text, now_text

# ---------------------------------------------------------------------------
# Documented limits and the shape of the API surface
# ---------------------------------------------------------------------------

#: Application asset kind -> the plural, lowercase entity_type string the
#: Entity Tag Assignments API expects. A view is a table to this API.
ENTITY_TYPE = {
    "catalog": "catalogs",
    "schema": "schemas",
    "table": "tables",
    "volume": "volumes",
}

#: Not a member of ENTITY_TYPE, and never derived from a Target: a column is
#: addressed by the four-part name plus this entity type.
COLUMN_ENTITY_TYPE = "columns"

#: Every entity type the API documents. Kept separate from ENTITY_TYPE so the
#: capability screen can show the full surface, columns included.
SUPPORTED_ENTITY_TYPES = ("catalogs", "schemas", "tables", "columns", "volumes")

#: Documented ceilings. Exceeding them is rejected by Databricks, not clamped.
MAX_TAGS_PER_OBJECT = 50
MAX_COLUMN_TAGS_PER_TABLE = 1000
MAX_POLICY_VALUES = 500
MAX_TAG_KEY_LENGTH = 256
MAX_TAG_VALUE_LENGTH = 256

#: One screen never pulls more than this. Well above the 50-tag ceiling for a
#: single object, and bounded for the 1000-column-tag case.
PAGE_LIMIT = 1200

#: Tag keys Databricks defines itself, produced by Data Classification. This
#: app reads them and refuses to create, change or delete their policies.
SYSTEM_TAG_PREFIX = "class."

#: Characters Databricks rejects in a tag key. This is the strictest documented
#: set across the tagging surfaces; being stricter than one endpoint is safe,
#: being looser turns into an opaque INVALID_PARAMETER_VALUE at apply time.
FORBIDDEN_KEY_CHARS = ".,-=/:*<>%&?\\"


# ---------------------------------------------------------------------------
# Honest explanations. The UI renders these verbatim; they are the difference
# between "there is nothing here" and "we are not allowed to know".
# ---------------------------------------------------------------------------

NO_INHERITANCE_NOTE = (
    "Thẻ KHÔNG lan xuống đối tượng con. Thẻ gắn trên catalog không hiện trên schema hay bảng "
    "bên trong, và thẻ trên bảng KHÔNG BAO GIỜ lan xuống cột. Kế thừa ngầm chỉ tồn tại khi "
    "chính sách ABAC được đánh giá, không phải trong danh sách thẻ của đối tượng."
)

REVERSE_LOOKUP_NOTE = (
    "Không thể tra ngược “những đối tượng nào đang mang thẻ X” bằng API này: API chỉ trả lời "
    "theo từng đối tượng. Muốn tra ngược phải truy vấn INFORMATION_SCHEMA bằng SQL Warehouse "
    "(ví dụ information_schema.catalog_tags / schema_tags / table_tags / column_tags / volume_tags). "
    "Ứng dụng không tự suy đoán danh sách này."
)

ABAC_IMPACT_NOTE = (
    "Thay đổi thẻ có thể làm thay đổi kết quả của chính sách ABAC (row filter / column mask) "
    "đang gắn theo thẻ. Dữ liệu có thể trở nên bị che hoặc hết bị che ngay sau thao tác này. "
    "Ứng dụng không xác định được có bao nhiêu chính sách bị ảnh hưởng."
)

ASSIGN_PERMISSION_NOTE = (
    "Gán hoặc gỡ thẻ cần đồng thời: APPLY_TAG trên chính đối tượng, USE SCHEMA trên schema cha "
    "và USE CATALOG trên catalog cha (hoặc là chủ sở hữu). Thiếu USE CATALOG là nguyên nhân phổ biến "
    "nhất của lỗi từ chối quyền ở đây, dù APPLY_TAG đã được cấp."
)

GOVERNED_TAG_PERMISSION_NOTE = (
    "Governed tag cần thêm một lớp quyền riêng: ASSIGN hoặc MANAGE trên chính tag policy. "
    "Quyền này được cấp qua Account Access Control Proxy API ở cấp account, KHÔNG phải bằng lệnh "
    "GRANT của Unity Catalog. SDK không có phương thức đọc hoặc đặt quyền trên w.tag_policies, "
    "nên ứng dụng không hiển thị và không thay đổi được lớp quyền này."
)

UNSUPPORTED_ENTITY_NOTE = (
    "API Entity Tag Assignments chỉ hỗ trợ catalog, schema, bảng/view, cột và volume. "
    "Hàm (function), mô hình đã đăng ký và phiên bản mô hình KHÔNG gắn thẻ được qua API này, "
    "kể cả khi giao diện Databricks cho phép. Với các đối tượng đó, thao tác trực tiếp trong "
    "Catalog Explorer."
)

SYSTEM_TAG_NOTE = (
    "Thẻ thuộc họ “class.” do Databricks định nghĩa và do Data Classification sinh ra. "
    "Ứng dụng chỉ đọc; không tạo, sửa hoặc xoá tag policy của họ thẻ này."
)

IMMUTABLE_KEY_NOTE = (
    "Không đổi được tên thẻ, loại đối tượng hay tên đối tượng của một lần gán. "
    "Chỉ sửa được giá trị (tag_value). Muốn đổi tên thẻ thì phải gỡ rồi gán lại."
)

LIMITS_NOTE = (
    f"Giới hạn Databricks: tối đa {MAX_TAGS_PER_OBJECT} thẻ trên một đối tượng, "
    f"tối đa {MAX_COLUMN_TAGS_PER_TABLE} thẻ cột trên một bảng, "
    f"tối đa {MAX_POLICY_VALUES} giá trị cho phép trên một governed tag. "
    f"Khoá và giá trị tối đa {MAX_TAG_KEY_LENGTH} ký tự và phân biệt chữ hoa chữ thường."
)

CLASSIFICATION_NOTE = (
    "Data Classification đang ở mức Public Preview, cần serverless compute và được tính phí riêng. "
    "Bật tự động gắn thẻ KHÔNG gắn thẻ cho dữ liệu đã có từ trước (không backfill); chỉ áp dụng cho "
    "các lần quét sau đó. Databricks KHÔNG cung cấp API để kích hoạt một lần quét hay để đọc kết quả "
    "phát hiện, nên ứng dụng không có nút “quét ngay”. Kết quả nằm ở bảng hệ thống "
    "system.data_classification.results và phải đọc bằng SQL Warehouse."
)

CLASSIFICATION_SCOPE_NOTE = (
    "included_schemas và excluded_schemas loại trừ lẫn nhau: chỉ được đặt một trong hai, "
    "và nếu đặt thì danh sách không được rỗng. Bỏ trống cả hai nghĩa là áp dụng cho toàn bộ catalog."
)

EMPTY_VS_DENIED_NOTE = (
    "Danh sách rỗng ở đây nghĩa là đối tượng thực sự chưa có thẻ nào, không phải do thiếu quyền. "
    "Trường hợp thiếu quyền luôn được báo bằng thông báo lỗi riêng."
)


def is_system_tag(key: str) -> bool:
    """Is this key part of the Databricks-defined ``class.*`` family?

    Matched case-insensitively on purpose: the comparison guards a refusal, and
    refusing a near-miss costs a steward one rename, while accepting one would
    let this app write into a namespace Databricks owns.
    """
    return (key or "").strip().lower().startswith(SYSTEM_TAG_PREFIX)


def validate_tag_key(key: str) -> str:
    """Reject anything Databricks would reject, before a request is built."""
    if key is None or not str(key):
        raise GovernanceError(Code.INVALID_INPUT, "Nhập khoá thẻ (tag key).")
    key = str(key)
    if key != key.strip():
        raise GovernanceError(
            Code.INVALID_INPUT,
            "Khoá thẻ không được bắt đầu hoặc kết thúc bằng khoảng trắng. "
            "Khoá thẻ phân biệt chữ hoa chữ thường nên ứng dụng không tự cắt bỏ giúp.",
        )
    if len(key) > MAX_TAG_KEY_LENGTH:
        raise GovernanceError(
            Code.INVALID_INPUT, f"Khoá thẻ tối đa {MAX_TAG_KEY_LENGTH} ký tự."
        )
    bad = sorted({c for c in key if c in FORBIDDEN_KEY_CHARS or ord(c) < 32})
    if bad:
        shown = ", ".join(repr(c) if ord(c) < 32 else c for c in bad)
        raise GovernanceError(
            Code.INVALID_INPUT,
            f"Khoá thẻ chứa ký tự không hợp lệ: {shown}. "
            f"Không dùng các ký tự {FORBIDDEN_KEY_CHARS} và ký tự điều khiển.",
        )
    return key


def validate_tag_value(value: str | None) -> str:
    """A tag may legitimately have no value; a control character is never legal."""
    if value is None:
        return ""
    value = str(value)
    if len(value) > MAX_TAG_VALUE_LENGTH:
        raise GovernanceError(
            Code.INVALID_INPUT, f"Giá trị thẻ tối đa {MAX_TAG_VALUE_LENGTH} ký tự."
        )
    if any(ord(c) < 32 for c in value):
        raise GovernanceError(Code.INVALID_INPUT, "Giá trị thẻ chứa ký tự điều khiển.")
    return value


def _time_text(value: Any) -> str:
    """Timestamps arrive as epoch millis on some fields and RFC3339 on others."""
    if not value:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return millis_to_text(value)
    text = str(value)
    if text.isdigit():
        return millis_to_text(text)
    return text


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


@dataclass
class TagRow:
    """One tag assignment on one entity."""

    key: str
    value: str = ""
    entity_type: str = ""
    entity_name: str = ""
    source_type: str = ""
    updated: str = ""
    updated_by: str = ""
    #: ``None`` means "not checked": governance status needs the tag policy
    #: listing, which the caller may not be allowed to read.
    governed: bool | None = None

    @property
    def system(self) -> bool:
        return is_system_tag(self.key)

    @property
    def governance_label(self) -> str:
        if self.system:
            return "Do Databricks sinh ra (class.*)"
        if self.governed is None:
            return "Chưa xác định"
        return "Governed tag" if self.governed else "Thẻ tự do"

    def row(self) -> dict:
        return {
            "Khoá": self.key,
            "Giá trị": self.value if self.value else "(không có giá trị)",
            "Loại thẻ": self.governance_label,
            "Nguồn": self.source_type or "—",
            "Cập nhật lần cuối": self.updated or "—",
            "Người cập nhật": self.updated_by or "—",
        }


@dataclass
class TagPolicyRow:
    """One governed tag, as defined at account level."""

    key: str
    description: str = ""
    values: list[str] = field(default_factory=list)
    policy_id: str = ""
    created: str = ""
    updated: str = ""

    @property
    def system(self) -> bool:
        return is_system_tag(self.key)

    @property
    def constrained(self) -> bool:
        """A policy with no values[] accepts any value."""
        return bool(self.values)

    def allows(self, value: str) -> bool:
        if not self.constrained:
            return True
        return value in self.values

    def row(self) -> dict:
        return {
            "Khoá": self.key,
            "Mô tả": self.description or "—",
            "Giá trị cho phép": ", ".join(self.values) if self.values else "Không giới hạn",
            "Số giá trị": len(self.values),
            "Do Databricks định nghĩa": "Có" if self.system else "Không",
            "Cập nhật lần cuối": self.updated or "—",
        }


@dataclass
class ClassificationView:
    """Data Classification configuration for one catalog."""

    catalog: str
    configured: bool = False
    included_schemas: list[str] = field(default_factory=list)
    #: One row per classification tag: {"tag": ..., "mode": ...}. Auto-tagging
    #: is configured per tag, not as a single on/off switch for the catalog.
    auto_tag_configs: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    observed_at: str = ""
    error: GovernanceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def enabled_tags(self) -> list[str]:
        return [c["tag"] for c in self.auto_tag_configs if c.get("enabled")]

    @property
    def scope_label(self) -> str:
        if self.included_schemas:
            return "Chỉ các schema: " + ", ".join(self.included_schemas)
        return "Toàn bộ catalog"

    @property
    def status_label(self) -> str:
        if self.error is not None:
            return "Không đọc được"
        if not self.configured:
            return "Chưa cấu hình cho catalog này"
        enabled = self.enabled_tags
        if not enabled:
            return "Đã cấu hình, chưa bật tự động gắn thẻ cho thẻ nào"
        return f"Đang bật tự động gắn thẻ cho {len(enabled)} thẻ"

    def summary_fields(self) -> list[tuple[str, str]]:
        return [
            ("Catalog", self.catalog),
            ("Trạng thái", self.status_label),
            ("Phạm vi quét", self.scope_label if self.configured else "—"),
            ("Quét theo yêu cầu", "Không có API — Databricks tự lên lịch quét"),
        ]

    def rows(self) -> list[dict]:
        return [
            {
                "Thẻ phân loại": c.get("tag", ""),
                "Tự động gắn thẻ": "Bật" if c.get("enabled") else "Tắt",
            }
            for c in self.auto_tag_configs
        ]


class TagService(Service):
    """Tags, governed tags and Data Classification for one workspace."""

    capability_keys = ("tags.read", "tags.write", "tags.policies", "classification.config")

    #: Re-exported so a screen can show the caveats without importing the module
    #: internals and without re-typing them.
    NO_INHERITANCE_NOTE = NO_INHERITANCE_NOTE
    REVERSE_LOOKUP_NOTE = REVERSE_LOOKUP_NOTE
    ABAC_IMPACT_NOTE = ABAC_IMPACT_NOTE
    ASSIGN_PERMISSION_NOTE = ASSIGN_PERMISSION_NOTE
    GOVERNED_TAG_PERMISSION_NOTE = GOVERNED_TAG_PERMISSION_NOTE
    UNSUPPORTED_ENTITY_NOTE = UNSUPPORTED_ENTITY_NOTE
    SYSTEM_TAG_NOTE = SYSTEM_TAG_NOTE
    IMMUTABLE_KEY_NOTE = IMMUTABLE_KEY_NOTE
    LIMITS_NOTE = LIMITS_NOTE
    CLASSIFICATION_NOTE = CLASSIFICATION_NOTE
    CLASSIFICATION_SCOPE_NOTE = CLASSIFICATION_SCOPE_NOTE
    EMPTY_VS_DENIED_NOTE = EMPTY_VS_DENIED_NOTE

    # -- addressing -------------------------------------------------------
    def entity_type_for(self, target: Target) -> str:
        """Target kind -> the plural entity_type string, or a clear refusal."""
        entity_type = ENTITY_TYPE.get(target.kind)
        if entity_type:
            return entity_type
        if target.kind in ("function", "model"):
            raise GovernanceError(
                Code.CAPABILITY_UNAVAILABLE,
                f"{target.label} không gắn thẻ được qua API. " + UNSUPPORTED_ENTITY_NOTE,
            )
        raise GovernanceError(
            Code.CAPABILITY_UNAVAILABLE,
            f"Loại đối tượng “{target.label}” không nằm trong phạm vi gắn thẻ của Unity Catalog. "
            + UNSUPPORTED_ENTITY_NOTE,
        )

    def can_tag(self, target: Target) -> tuple[bool, str]:
        """Advisory check for the UI, so a screen can grey out instead of failing."""
        try:
            self.entity_type_for(target)
        except GovernanceError as err:
            return False, err.message
        return True, ""

    def entity_ref(self, target: Target, column: str = "") -> tuple[str, str]:
        """Resolve (entity_type, entity_name), including the column case.

        A column is not a Target: Unity Catalog does not expose it as a
        securable. It is addressed here as entity_type ``columns`` plus the
        four-part name, which is the only form the API accepts.
        """
        self.authz.require_scope(target.catalog)
        if column:
            if target.kind != "table":
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    "Chỉ bảng hoặc view mới có cột để gắn thẻ.",
                )
            validate_component(column, "Tên cột")
            return COLUMN_ENTITY_TYPE, f"{target.full_name}.{column}"
        return self.entity_type_for(target), target.full_name

    #: Plural entity_type -> the noun a Vietnamese-speaking steward uses.
    ENTITY_NOUN = {
        "catalogs": "Catalog",
        "schemas": "Schema",
        "tables": "Bảng / View",
        "columns": "Cột",
        "volumes": "Volume",
    }

    @classmethod
    def entity_noun(cls, entity_type: str) -> str:
        return cls.ENTITY_NOUN.get(entity_type, entity_type)

    @classmethod
    def entity_label(cls, entity_type: str, entity_name: str) -> str:
        return f"{cls.entity_noun(entity_type)} {entity_name}"

    # -- error shaping ----------------------------------------------------
    def _tag_error(self, exc: BaseException, *, mutating: bool = False,
                   governed: bool = False) -> GovernanceError:
        """Translate, then add the two hints the raw 403 never contains.

        Nothing from the upstream body is copied in: only our own text is
        appended to the message ``translate`` already produced.
        """
        err = translate(exc, mutating=mutating)
        if err.code != Code.PERMISSION_DENIED:
            return err
        extra = ASSIGN_PERMISSION_NOTE
        if governed:
            extra = extra + " " + GOVERNED_TAG_PERMISSION_NOTE
        return GovernanceError(
            code=err.code,
            message=err.message + " " + extra,
            correlation_id=err.correlation_id,
            detail=err.detail,
        )

    # -- reading tags -----------------------------------------------------
    def _assignments(self, entity_type: str, entity_name: str,
                     governed_keys: frozenset[str] | None) -> tuple[list[TagRow], bool]:
        # The SDK iterator is used rather than hand-rolled paging because this
        # endpoint can return an EMPTY page that still carries a next token;
        # stopping at the first empty page would hide tags that do exist.
        iterator = self.w.entity_tag_assignments.list(
            entity_type=entity_type, entity_name=entity_name
        )
        items, more = take(iterator, PAGE_LIMIT)
        rows: list[TagRow] = []
        for item in items:
            data = as_dict(item)
            key = data.get("tag_key") or ""
            rows.append(TagRow(
                key=key,
                value=data.get("tag_value") or "",
                entity_type=data.get("entity_type") or entity_type,
                entity_name=data.get("entity_name") or entity_name,
                source_type=enum_value(data.get("source_type")),
                updated=_time_text(data.get("update_time")),
                updated_by=data.get("updated_by") or "",
                governed=None if governed_keys is None else key in governed_keys,
            ))
        rows.sort(key=lambda r: (not r.system, r.key))
        return rows, more

    def list_tags(self, target: Target, *, with_governance: bool = True) -> Listing:
        """Tags assigned directly to this object.

        Directly is the only kind there is: see :data:`NO_INHERITANCE_NOTE`.
        """
        def run() -> Listing:
            # Resolved inside the listing so an unsupported object kind explains
            # itself in the panel instead of taking the whole screen down.
            entity_type, entity_name = self.entity_ref(target)
            governed_keys = self._governed_keys() if with_governance else None
            try:
                rows, more = self._assignments(entity_type, entity_name, governed_keys)
            except Exception as exc:
                raise self._tag_error(exc) from None
            note = NO_INHERITANCE_NOTE if rows else EMPTY_VS_DENIED_NOTE
            if governed_keys is None:
                note = note + " " + (
                    "Chưa xác định được thẻ nào là governed tag vì không đọc được danh sách tag policy."
                )
            return Listing(
                items=rows,
                completeness=Completeness.TRUNCATED if more else Completeness.COMPLETE,
                observed_at=now_text(),
                note=note,
            )

        return self.listing(run)

    def list_column_tags(self, target: Target, column: str,
                         *, with_governance: bool = True) -> Listing:
        """Tags on one column of a table or view.

        Column tags never inherit from the table, so this listing is genuinely
        independent of :meth:`list_tags` on the same object.
        """
        def run() -> Listing:
            entity_type, entity_name = self.entity_ref(target, column=column)
            governed_keys = self._governed_keys() if with_governance else None
            try:
                rows, more = self._assignments(entity_type, entity_name, governed_keys)
            except Exception as exc:
                raise self._tag_error(exc) from None
            note = (
                "Thẻ cột hoàn toàn độc lập với thẻ của bảng cha: không có kế thừa theo chiều nào."
                if rows else EMPTY_VS_DENIED_NOTE
            )
            return Listing(
                items=rows,
                completeness=Completeness.TRUNCATED if more else Completeness.COMPLETE,
                observed_at=now_text(),
                note=note,
            )

        return self.listing(run)

    def tag_value(self, target: Target, key: str, *, column: str = "") -> str | None:
        """Current value of one tag, or ``None`` when it is not assigned.

        ``None`` means "not assigned". An empty string means "assigned with no
        value", which is a different state and must stay distinguishable.
        """
        entity_type, entity_name = self.entity_ref(target, column=column)
        key = validate_tag_key(key)
        try:
            assignment = self.w.entity_tag_assignments.get(
                entity_type=entity_type, entity_name=entity_name, tag_key=key
            )
        except Exception as exc:
            err = self._tag_error(exc)
            if err.code == Code.NOT_FOUND:
                return None
            raise err from None
        return as_dict(assignment).get("tag_value") or ""

    # -- governed tags ----------------------------------------------------
    def governed_tags(self) -> Listing:
        """Tag policies defined at account level for this metastore."""

        def run() -> Listing:
            try:
                items, more = take(self.w.tag_policies.list_tag_policies(), PAGE_LIMIT)
            except Exception as exc:
                raise self._tag_error(exc, governed=True) from None
            rows = [self._policy_row(p) for p in items]
            rows.sort(key=lambda r: (r.system, r.key.lower()))
            return Listing(
                items=rows,
                completeness=Completeness.TRUNCATED if more else Completeness.COMPLETE,
                observed_at=now_text(),
                note=GOVERNED_TAG_PERMISSION_NOTE + " " + SYSTEM_TAG_NOTE,
            )

        return self.listing(run)

    def governed_tag(self, key: str) -> TagPolicyRow | None:
        """One tag policy, or ``None`` when the key is not governed."""
        key = validate_tag_key(key)
        try:
            policy = self.w.tag_policies.get_tag_policy(tag_key=key)
        except Exception as exc:
            err = self._tag_error(exc, governed=True)
            if err.code == Code.NOT_FOUND:
                return None
            raise err from None
        return self._policy_row(policy)

    def _governed_keys(self) -> frozenset[str] | None:
        """Keys under policy, or ``None`` when the listing was not readable.

        ``None`` is deliberately not an empty set: "no governed tags exist" and
        "we are not allowed to know" must not render the same way.
        """
        try:
            items, _ = take(self.w.tag_policies.list_tag_policies(), PAGE_LIMIT)
        except Exception:
            return None
        return frozenset(
            (as_dict(p).get("tag_key") or "") for p in items if as_dict(p).get("tag_key")
        )

    @staticmethod
    def _policy_row(policy: Any) -> TagPolicyRow:
        data = as_dict(policy)
        values = []
        for value in data.get("values") or []:
            name = as_dict(value).get("name") if not isinstance(value, str) else value
            if name:
                values.append(str(name))
        return TagPolicyRow(
            key=data.get("tag_key") or "",
            description=data.get("description") or "",
            values=values,
            policy_id=str(data.get("id") or ""),
            created=_time_text(data.get("create_time")),
            updated=_time_text(data.get("update_time")),
        )

    def allowed_values(self, key: str) -> tuple[bool, list[str], str]:
        """``(governed, allowed_values, note)`` for one key.

        The note is what the UI shows when a value is refused, so a steward sees
        the permitted list instead of a bare validation failure.
        """
        policy = self.governed_tag(key)
        if policy is None:
            return False, [], "Thẻ tự do: giá trị không bị ràng buộc bởi tag policy."
        if not policy.constrained:
            return True, [], (
                f"Governed tag “{policy.key}” không giới hạn danh sách giá trị. "
                + GOVERNED_TAG_PERMISSION_NOTE
            )
        return True, list(policy.values), (
            f"Governed tag “{policy.key}” chỉ nhận các giá trị: " + ", ".join(policy.values)
        )

    # -- planning: assignments --------------------------------------------
    def plan_assign(
        self,
        target: Target,
        key: str,
        value: str,
        reason: str,
        *,
        column: str = "",
    ) -> Plan:
        """Validate -> Authorize -> Build plan -> Preview. Nothing is sent."""
        self.authz.require(Action.ASSIGN_TAG, target)
        entity_type, entity_name = self.entity_ref(target, column=column)
        key = validate_tag_key(key)
        value = validate_tag_value(value)
        reason = self._reason(reason)

        current = self._current_state(entity_type, entity_name)
        existing = current.get(key)

        if existing is not None and existing == value:
            raise GovernanceError(
                Code.ALREADY_SATISFIED,
                f"Thẻ “{key}” trên {entity_name} đã có đúng giá trị này.",
            )
        if existing is None and len(current) >= MAX_TAGS_PER_OBJECT:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"{self.entity_label(entity_type, entity_name)} đã có {len(current)} thẻ, "
                f"đạt giới hạn {MAX_TAGS_PER_OBJECT}. Gỡ bớt một thẻ trước khi gán thêm.",
            )

        governed, policy_note, policy_known = self._policy_check(key, value)

        operation = "update" if existing is not None else "create"
        plan = Plan(
            action=Action.ASSIGN_TAG,
            target_key=self._plan_key(target, entity_type, entity_name, key),
            target_name=entity_name,
            target_type=self.entity_noun(entity_type),
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload={
                "operation": operation,
                "entity_type": entity_type,
                "entity_name": entity_name,
                "tag_key": key,
                "tag_value": value,
                "previous_value": existing,
                "governed": governed,
            },
            before=self._assignment_fingerprint(key, existing),
            reason=reason,
            summary=(
                f"Sửa giá trị thẻ {key}" if existing is not None else f"Gán thẻ {key}"
            ),
        )
        plan.preview = self._assign_preview(
            entity_type, entity_name, key, value, existing,
            governed, policy_note, policy_known,
        )
        return plan

    def plan_remove(
        self,
        target: Target,
        key: str,
        reason: str,
        *,
        column: str = "",
    ) -> Plan:
        """Plan the removal of one tag assignment."""
        self.authz.require(Action.ASSIGN_TAG, target)
        entity_type, entity_name = self.entity_ref(target, column=column)
        key = validate_tag_key(key)
        reason = self._reason(reason)

        current = self._current_state(entity_type, entity_name)
        if key not in current:
            raise GovernanceError(
                Code.NOT_FOUND,
                f"{self.entity_label(entity_type, entity_name)} không mang thẻ “{key}”.",
            )
        existing = current[key]
        governed_keys = self._governed_keys()
        governed = None if governed_keys is None else key in governed_keys

        plan = Plan(
            action=Action.ASSIGN_TAG,
            target_key=self._plan_key(target, entity_type, entity_name, key),
            target_name=entity_name,
            target_type=self.entity_noun(entity_type),
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload={
                "operation": "delete",
                "entity_type": entity_type,
                "entity_name": entity_name,
                "tag_key": key,
                "previous_value": existing,
                "governed": governed,
                # Removing a tag can switch off an ABAC policy that depends on
                # it, so this one asks the operator to retype the object name.
                "confirm_required": True,
            },
            before=self._assignment_fingerprint(key, existing),
            reason=reason,
            summary=f"Gỡ thẻ {key}",
        )
        plan.preview = [
            PreviewLine(f"Gỡ thẻ: {key}"
                        + (f" (giá trị hiện tại: {existing})" if existing else ""), "change"),
            PreviewLine(f"Khỏi: {self.entity_label(entity_type, entity_name)}", "change"),
            self._identity_line(),
            PreviewLine(ABAC_IMPACT_NOTE, "warning"),
            PreviewLine(
                "Gỡ thẻ có thể làm MẤT lớp bảo vệ: nếu một chính sách ABAC che dữ liệu dựa trên thẻ này, "
                "dữ liệu sẽ hiện ra với những người trước đó bị che.",
                "warning",
            ),
            PreviewLine(IMMUTABLE_KEY_NOTE, "info"),
            PreviewLine(
                "Số chính sách ABAC phụ thuộc thẻ này: Chưa xác định — Unity Catalog không cung cấp "
                "phép tra ngược và ứng dụng không ước lượng.",
                "unknown",
            ),
        ]
        if governed is True:
            plan.preview.append(PreviewLine(GOVERNED_TAG_PERMISSION_NOTE, "warning"))
        elif governed is None:
            plan.preview.append(PreviewLine(
                "Chưa xác định được thẻ này có phải governed tag hay không (không đọc được tag policy). "
                + GOVERNED_TAG_PERMISSION_NOTE,
                "unknown",
            ))
        if is_system_tag(key):
            plan.preview.append(PreviewLine(
                "Đây là thẻ do Data Classification sinh ra. Nếu tự động gắn thẻ vẫn bật, "
                "Databricks có thể gắn lại thẻ này ở lần quét sau.",
                "warning",
            ))
        return plan

    # -- planning: tag policies -------------------------------------------
    def plan_policy_upsert(
        self,
        key: str,
        description: str,
        values: list[str],
        reason: str,
    ) -> Plan:
        """Create or change one governed tag definition."""
        self.authz.require(Action.MANAGE_TAG_POLICY, None)
        key = validate_tag_key(key)
        self._refuse_system_tag(key)
        reason = self._reason(reason)
        description = validate_tag_value(description) if description else ""

        chosen: list[str] = []
        for value in values or []:
            text = validate_tag_value(value)
            if text and text not in chosen:
                chosen.append(text)
        if len(chosen) > MAX_POLICY_VALUES:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Một governed tag chỉ cho phép tối đa {MAX_POLICY_VALUES} giá trị, "
                f"đang chọn {len(chosen)}.",
            )

        existing = self.governed_tag(key)
        operation = "policy_update" if existing else "policy_create"

        # update_tag_policy is a masked patch: only the paths named here are
        # touched, so the mask is built from what actually differs.
        mask: list[str] = []
        if existing is None:
            mask = ["description", "values"]
        else:
            if description != existing.description:
                mask.append("description")
            if chosen != existing.values:
                mask.append("values")
            if not mask:
                raise GovernanceError(
                    Code.ALREADY_SATISFIED,
                    f"Governed tag “{key}” đã đúng như mô tả và danh sách giá trị đã nhập.",
                )

        removed = [v for v in (existing.values if existing else []) if v not in chosen]

        plan = Plan(
            action=Action.MANAGE_TAG_POLICY,
            target_key=f"tag_policy:{key}",
            target_name=key,
            target_type="Governed tag",
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload={
                "operation": operation,
                "tag_key": key,
                "description": description,
                "values": chosen,
                "update_mask": ",".join(mask),
            },
            before=self._policy_fingerprint(existing),
            reason=reason,
            summary=("Tạo governed tag " if existing is None else "Cập nhật governed tag ") + key,
        )
        plan.preview = [
            PreviewLine(
                ("Tạo governed tag: " if existing is None else "Cập nhật governed tag: ") + key,
                "change",
            ),
            PreviewLine(
                "Giá trị cho phép: " + (", ".join(chosen) if chosen else "Không giới hạn"),
                "change",
            ),
            self._identity_line(),
            PreviewLine(GOVERNED_TAG_PERMISSION_NOTE, "warning"),
            PreviewLine(ABAC_IMPACT_NOTE, "warning"),
        ]
        if existing is not None:
            plan.preview.insert(2, PreviewLine(
                "Chỉ gửi các trường thay đổi (update_mask: "
                + ", ".join(mask) + "); các trường khác giữ nguyên.",
                "info",
            ))
        if removed:
            plan.preview.append(PreviewLine(
                "Bỏ khỏi danh sách cho phép: " + ", ".join(removed)
                + ". Các đối tượng đang mang giá trị này sẽ không được ứng dụng tự sửa, và "
                "Unity Catalog không cung cấp cách liệt kê chúng.",
                "warning",
            ))
        return plan

    def plan_policy_delete(self, key: str, reason: str) -> Plan:
        """Plan the deletion of one governed tag definition."""
        self.authz.require(Action.MANAGE_TAG_POLICY, None)
        key = validate_tag_key(key)
        self._refuse_system_tag(key)
        reason = self._reason(reason)

        existing = self.governed_tag(key)
        if existing is None:
            raise GovernanceError(Code.NOT_FOUND, f"Không có governed tag nào tên “{key}”.")

        plan = Plan(
            action=Action.MANAGE_TAG_POLICY,
            target_key=f"tag_policy:{key}",
            target_name=key,
            target_type="Governed tag",
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload={
                "operation": "policy_delete",
                "tag_key": key,
                "confirm_required": True,
            },
            before=self._policy_fingerprint(existing),
            reason=reason,
            summary=f"Xoá governed tag {key}",
        )
        plan.preview = [
            PreviewLine(f"Xoá định nghĩa governed tag: {key}", "change"),
            self._identity_line(),
            PreviewLine(
                "Xoá tag policy sẽ bỏ ràng buộc giá trị và bỏ lớp quyền ASSIGN/MANAGE gắn với thẻ này.",
                "warning",
            ),
            PreviewLine(ABAC_IMPACT_NOTE, "warning"),
            PreviewLine(
                "Các lần gán thẻ đã có trên đối tượng: Chưa xác định — ứng dụng không liệt kê ngược được, "
                "và không xoá chúng thay bạn.",
                "unknown",
            ),
        ]
        return plan

    #: Why Data Classification is read-only here.
    #:
    #: ``CatalogConfig`` carries no catalog-level on/off switch: auto-tagging is
    #: configured per classification tag through ``auto_tag_configs``, the scan
    #: scope is an include-list only, and the effective state also depends on a
    #: metastore-level setting that no documented API exposes. Writing a partial
    #: model of that through a two-field form would produce a control that
    #: silently means something other than what it says, so the app reports the
    #: configuration and sends the operator to Databricks to change it.
    CLASSIFICATION_READ_ONLY = (
        "Ứng dụng chỉ hiển thị cấu hình Data Classification, không thay đổi. "
        "Tự động gắn thẻ được cấu hình riêng cho từng thẻ phân loại và còn phụ thuộc "
        "một thiết lập ở cấp metastore mà Databricks chưa công bố API. "
        "Hãy bật/tắt trong Catalog Explorer để tránh cấu hình sai lệch."
    )

    # -- executing --------------------------------------------------------
    def apply(self, plan: Plan, target: Target | None = None,
              confirmation: str | None = None):
        """The single execution path for everything this service changes.

        Dispatch happens on ``payload["operation"]`` so that assignment, policy
        and classification changes all inherit the same revalidate/verify/audit
        discipline instead of growing their own shortcuts.
        """
        if plan.payload.get("confirm_required"):
            plan.require_confirmation(confirmation or "")

        operation = plan.payload.get("operation", "")
        if operation in ("create", "update", "delete"):
            return self._apply_assignment(plan, target)
        if operation in ("policy_create", "policy_update", "policy_delete"):
            return self._apply_policy(plan)
        raise GovernanceError(Code.INVALID_INPUT, "Bản xem trước không hợp lệ cho dịch vụ thẻ.")

    def _apply_assignment(self, plan: Plan, target: Target | None):
        from databricks.sdk.service.catalog import EntityTagAssignment

        payload = plan.payload
        entity_type = payload["entity_type"]
        entity_name = payload["entity_name"]
        key = payload["tag_key"]
        value = payload.get("tag_value", "")
        operation = payload["operation"]
        governed = payload.get("governed") is True

        def revalidate() -> str:
            current = self._current_state(entity_type, entity_name)
            return self._assignment_fingerprint(key, current.get(key))

        def do():
            try:
                if operation == "delete":
                    self.w.entity_tag_assignments.delete(
                        entity_type=entity_type, entity_name=entity_name, tag_key=key
                    )
                elif operation == "update":
                    # Only tag_value is mutable, and the mask is mandatory:
                    # entity_name, entity_type and tag_key cannot be patched.
                    self.w.entity_tag_assignments.update(
                        entity_type=entity_type,
                        entity_name=entity_name,
                        tag_key=key,
                        tag_assignment=EntityTagAssignment(
                            entity_type=entity_type,
                            entity_name=entity_name,
                            tag_key=key,
                            tag_value=value or None,
                        ),
                        update_mask="tag_value",
                    )
                else:
                    self.w.entity_tag_assignments.create(
                        tag_assignment=EntityTagAssignment(
                            entity_type=entity_type,
                            entity_name=entity_name,
                            tag_key=key,
                            tag_value=value or None,
                        )
                    )
            except Exception as exc:
                raise self._tag_error(exc, mutating=True, governed=governed) from None

        def verify() -> dict:
            current = self._current_state(entity_type, entity_name)
            now = current.get(key)
            if operation == "delete":
                return {
                    "tag_key": key,
                    "present": key in current,
                    "applied": key not in current,
                }
            return {
                "tag_key": key,
                "value_now": now,
                "applied": now == value,
            }

        return self.execute(
            plan, target, do,
            revalidate=revalidate, verify=verify,
            action_label=plan.summary,
        )

    def _apply_policy(self, plan: Plan):
        from databricks.sdk.service.tags import TagPolicy, Value

        payload = plan.payload
        key = payload["tag_key"]
        operation = payload["operation"]

        def revalidate() -> str:
            return self._policy_fingerprint(self.governed_tag(key))

        def do():
            try:
                if operation == "policy_delete":
                    self.w.tag_policies.delete_tag_policy(tag_key=key)
                    return
                values = [Value(name=v) for v in payload.get("values") or []]
                policy = TagPolicy(
                    tag_key=key,
                    description=payload.get("description") or None,
                    values=values or None,
                )
                if operation == "policy_update":
                    self.w.tag_policies.update_tag_policy(
                        tag_key=key,
                        tag_policy=policy,
                        update_mask=payload.get("update_mask") or "description,values",
                    )
                else:
                    self.w.tag_policies.create_tag_policy(tag_policy=policy)
            except Exception as exc:
                raise self._tag_error(exc, mutating=True, governed=True) from None

        def verify() -> dict:
            after = self.governed_tag(key)
            if operation == "policy_delete":
                return {"tag_key": key, "present": after is not None, "applied": after is None}
            return {
                "tag_key": key,
                "values_now": list(after.values) if after else [],
                "applied": after is not None
                and after.values == list(payload.get("values") or [])
                and after.description == (payload.get("description") or ""),
            }

        return self.execute(
            plan, None, do,
            revalidate=revalidate, verify=verify,
            action_label=plan.summary,
        )

    # -- data classification reads ----------------------------------------
    def classification_config(self, catalog: str) -> ClassificationView:
        """Current Data Classification configuration for one catalog.

        ``configured=False`` with no error means Databricks has no config for
        this catalog yet, which is not the same as a refused read; that case
        arrives with ``error`` set.
        """
        catalog = validate_component(catalog, "Tên catalog")
        self.authz.require_scope(catalog)
        view = ClassificationView(catalog=catalog, observed_at=now_text())
        try:
            # get/update/delete address the config resource itself; only create
            # addresses the parent catalog. Mixing the two forms is a 404.
            config = self.w.data_classification.get_catalog_config(
                name=f"catalogs/{catalog}/config"
            )
        except Exception as exc:
            err = translate(exc)
            if err.code == Code.NOT_FOUND:
                return view
            view.error = err
            return view

        data = as_dict(config)
        view.raw = data
        view.configured = True
        # CatalogConfig carries no single on/off flag: auto-tagging is set per
        # classification tag through auto_tag_configs, and the scan scope is an
        # include-list only (there is no exclude-list on this payload).
        included = data.get("included_schemas") or {}
        if isinstance(included, dict):
            view.included_schemas = [str(s) for s in (included.get("names") or [])]
        elif isinstance(included, list):
            view.included_schemas = [str(s) for s in included]
        for entry in data.get("auto_tag_configs") or []:
            item = as_dict(entry)
            mode = str(enum_value(item.get("auto_tagging_mode")) or "")
            view.auto_tag_configs.append({
                "tag": str(item.get("classification_tag") or ""),
                "mode": mode,
                "enabled": mode.upper().endswith("ENABLED"),
            })
        return view

    def classification_results_note(self) -> str:
        """Why there is no detections panel, phrased for the screen that lacks one."""
        base = (
            "Ứng dụng không hiển thị kết quả phát hiện của Data Classification: Databricks không có "
            "API đọc kết quả. Dữ liệu nằm ở system.data_classification.results."
        )
        if not self.settings.warehouse_id:
            return base + " Cần cấu hình một SQL Warehouse mới truy vấn được bảng hệ thống này."
        return base + " Dùng SQL Warehouse đã cấu hình để truy vấn bảng hệ thống này."

    # -- shared helpers ---------------------------------------------------
    def _current_state(self, entity_type: str, entity_name: str) -> dict[str, str]:
        """key -> value for everything currently assigned to one entity."""
        try:
            rows, _ = self._assignments(entity_type, entity_name, None)
        except Exception as exc:
            raise self._tag_error(exc) from None
        return {r.key: r.value for r in rows}

    @staticmethod
    def _assignment_fingerprint(key: str, value: str | None) -> str:
        """Fingerprint only the tag being changed.

        Narrow on purpose: another steward tagging the same table with an
        unrelated key must not invalidate this preview, but a change to *this*
        key must.
        """
        return fingerprint({"tag_key": key, "assigned": value is not None, "tag_value": value})

    @staticmethod
    def _policy_fingerprint(policy: TagPolicyRow | None) -> str:
        if policy is None:
            return fingerprint({"exists": False})
        return fingerprint({
            "exists": True,
            "description": policy.description,
            "values": list(policy.values),
        })

    @staticmethod
    def _plan_key(target: Target, entity_type: str, entity_name: str, tag_key: str) -> str:
        # The column is part of the key: a preview built for one column must not
        # be applicable to another column of the same table.
        return f"tag:{target.key}:{entity_type}:{entity_name}:{tag_key}"

    @staticmethod
    def _reason(reason: str) -> str:
        reason = (reason or "").strip()
        if not reason:
            raise GovernanceError(Code.INVALID_INPUT, "Nhập lý do thay đổi.")
        if len(reason) > 500:
            raise GovernanceError(Code.INVALID_INPUT, "Lý do tối đa 500 ký tự.")
        return reason

    @staticmethod
    def _refuse_system_tag(key: str):
        if is_system_tag(key):
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"“{key}” thuộc họ thẻ hệ thống của Databricks. " + SYSTEM_TAG_NOTE,
            )

    def _identity_line(self) -> PreviewLine:
        return PreviewLine(
            "Yêu cầu sẽ được gửi bằng: "
            + ("tài khoản dịch vụ của ứng dụng"
               if self.ctx.execution_identity == "app_service_principal"
               else "hồ sơ đăng nhập hiện tại"),
            "info",
        )

    def _policy_check(self, key: str, value: str) -> tuple[bool | None, str, bool]:
        """Validate a value against its governed tag policy, locally.

        Returns ``(governed, note, policy_known)``. The value is checked here,
        before anything is sent, so a steward sees the permitted list instead of
        an opaque rejection from Databricks. When the policy is not readable,
        ``governed`` is ``None`` and the check is reported as not performed
        rather than silently treated as a pass.
        """
        try:
            policy = self.governed_tag(key)
        except GovernanceError as err:
            if err.code == Code.PERMISSION_DENIED:
                return None, (
                    "Không kiểm tra được ràng buộc governed tag vì danh tính thực thi không đọc được "
                    "tag policy. Databricks vẫn sẽ từ chối nếu giá trị không hợp lệ."
                ), False
            raise
        if policy is None:
            return False, "Thẻ tự do: không có tag policy ràng buộc giá trị.", True
        if not policy.constrained:
            return True, f"Governed tag “{key}” không giới hạn danh sách giá trị.", True
        if not policy.allows(value):
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Giá trị “{value}” không nằm trong danh sách cho phép của governed tag “{key}”. "
                f"Giá trị hợp lệ: {', '.join(policy.values)}.",
            )
        return True, (
            f"Giá trị hợp lệ theo tag policy “{key}”: " + ", ".join(policy.values)
        ), True

    def _assign_preview(
        self,
        entity_type: str,
        entity_name: str,
        key: str,
        value: str,
        existing: str | None,
        governed: bool | None,
        policy_note: str,
        policy_known: bool,
    ) -> list[PreviewLine]:
        verb = "Sửa giá trị thẻ" if existing is not None else "Gán thẻ"
        lines = [
            PreviewLine(
                f"{verb}: {key} = " + (value if value else "(không có giá trị)"), "change"
            ),
            PreviewLine(f"Trên: {self.entity_label(entity_type, entity_name)}", "change"),
            self._identity_line(),
        ]
        if existing is not None:
            lines.append(PreviewLine(
                "Giá trị hiện tại: " + (existing if existing else "(không có giá trị)")
                + ". Chỉ trường tag_value được gửi đi (update_mask: tag_value).",
                "info",
            ))
            lines.append(PreviewLine(IMMUTABLE_KEY_NOTE, "info"))
        lines.append(PreviewLine(ABAC_IMPACT_NOTE, "warning"))
        lines.append(PreviewLine(NO_INHERITANCE_NOTE, "info"))
        if entity_type == COLUMN_ENTITY_TYPE:
            lines.append(PreviewLine(
                "Đây là thẻ cột: nó không xuất hiện trong danh sách thẻ của bảng cha và cũng không "
                "được thừa hưởng từ bảng cha.",
                "info",
            ))
        if governed is True:
            lines.append(PreviewLine(policy_note, "info"))
            lines.append(PreviewLine(GOVERNED_TAG_PERMISSION_NOTE, "warning"))
        elif governed is False:
            lines.append(PreviewLine(policy_note, "info"))
        else:
            lines.append(PreviewLine(policy_note, "unknown"))
        if not policy_known:
            lines.append(PreviewLine(
                "Ràng buộc giá trị chưa được kiểm tra trước khi gửi.", "unknown"
            ))
        if is_system_tag(key):
            lines.append(PreviewLine(
                "Khoá thuộc họ “class.” do Databricks quản lý. Giá trị bạn đặt có thể bị Data "
                "Classification ghi đè ở lần quét sau.",
                "warning",
            ))
        lines.append(PreviewLine(LIMITS_NOTE, "info"))
        lines.append(PreviewLine(REVERSE_LOOKUP_NOTE, "unknown"))
        return lines
