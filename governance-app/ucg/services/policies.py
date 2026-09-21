"""ABAC policies: row filters and column masks driven by governed tags.

Unity Catalog protects columns and rows through **two unrelated mechanisms**
that this module deliberately keeps apart:

* **ABAC policies** (``w.policies.*``) - attached to a catalog, a schema or a
  table, matched against *governed tags*, and cascading down to every
  descendant table. This is the only mechanism the SDK can create or change.
* **Legacy bindings** (``ALTER TABLE … SET ROW FILTER`` /
  ``ALTER TABLE … ALTER COLUMN … SET MASK``) - attached to one table by SQL,
  surfacing as ``TableInfo.row_filter`` and ``ColumnInfo.mask``. There is no
  API for them at all, in any SDK version.

An ABAC policy never appears in ``TableInfo.row_filter``/``ColumnInfo.mask``,
and a legacy binding never appears in ``list_policies``. Reading one surface
and calling it "the protection on this table" is the central bug this module
exists to prevent, so :meth:`PolicyService.legacy_protections` is provided
alongside the policy listing and both carry :data:`TWO_MECHANISMS`.

Three further traps are encoded rather than documented-and-forgotten:

* ``when_condition`` is **table-level governed-tag matching**, not a row
  predicate, and an **empty one means TRUE** - a catalog-level policy with no
  ``when_condition`` hits every table under that catalog.
* ``update_policy``'s ``update_mask`` is a real field mask: an empty mask (or
  ``*``) applies every field of the supplied ``PolicyInfo`` and wipes whatever
  was not populated. This module never sends one.
* Only one row filter may resolve per (table, user) and one column mask per
  (column, user). Databricks compares the *functions*, so two different
  functions fail the query closed rather than picking a winner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from ..authz import Action
from ..errors import Code, GovernanceError, translate
from ..naming import (
    PIPELINE_MANAGED_TABLE_TYPES, Target, validate_component,
)
from ..paging import Completeness, take
from ..plans import Outcome, Plan, PreviewLine, fingerprint
from .base import Listing, Service, as_dict, enum_value, millis_to_text, now_text

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import
    from .assets import AssetDetail

#: Wire values of ``PolicyType``. Kept as strings so a listing can be rendered
#: without constructing SDK enums from possibly-unknown server values.
ROW_FILTER = "POLICY_TYPE_ROW_FILTER"
COLUMN_MASK = "POLICY_TYPE_COLUMN_MASK"

POLICY_TYPE_LABELS = {
    ROW_FILTER: "Row filter (lọc dòng)",
    COLUMN_MASK: "Column mask (che giá trị cột)",
}

#: Object kinds that can serve as an attach point. METASTORE is intentionally
#: absent: it is Beta and must be probed, never assumed. See
#: :meth:`PolicyService.metastore_attach`.
ATTACH_KINDS = ("catalog", "schema", "table")

#: Attach points whose policies fan out to objects the operator is not looking
#: at. Used to decide when a preview has to shout.
CASCADING_KINDS = frozenset({"metastore", "catalog", "schema"})

#: Documented quotas, by attach-point kind.
QUOTAS = {"metastore": 10_000, "catalog": 100, "schema": 100, "table": 50}

#: ``to_principals`` + ``except_principals`` combined.
MAX_PRINCIPALS = 20
#: A policy may carry at most three column matchers, ANDed together.
MAX_MATCH_COLUMNS = 3

#: How many policies one screen pulls before it says "thu hẹp phạm vi".
PAGE_LIMIT = 500

#: Logical field name -> (field-mask path, Vietnamese label). The mask this
#: module sends is always built from this map, never left empty.
UPDATABLE_FIELDS: dict[str, tuple[str, str]] = {
    "to_principals": ("to_principals", "Danh sách principal áp dụng"),
    "except_principals": ("except_principals", "Danh sách principal được loại trừ"),
    "when_condition": ("when_condition", "Điều kiện chọn bảng (when_condition)"),
    "match_columns": ("match_columns", "Bộ chọn cột theo thẻ (match_columns)"),
    "column_mask": ("column_mask", "Hàm che cột (column mask)"),
    "row_filter": ("row_filter", "Hàm lọc dòng (row filter)"),
    "comment": ("comment", "Mô tả"),
}

# --- Explanatory constants. Every one of these states a real Databricks limit;
#     the UI renders them verbatim instead of inventing reassuring wording. ---

TWO_MECHANISMS = (
    "Unity Catalog có HAI cơ chế che dữ liệu độc lập nhau:\n"
    "1. **Chính sách ABAC** — gắn ở catalog / schema / bảng, khớp theo governed tag, "
    "lan xuống mọi bảng con. Đây là cơ chế duy nhất API quản lý được.\n"
    "2. **Ràng buộc kiểu cũ (legacy)** — gắn trực tiếp vào một bảng bằng câu lệnh SQL "
    "`ALTER TABLE … SET ROW FILTER` / `ALTER TABLE … ALTER COLUMN … SET MASK`.\n"
    "Chính sách ABAC KHÔNG hiện trong `TableInfo.row_filter` / `ColumnInfo.mask`, và "
    "ràng buộc legacy KHÔNG hiện trong danh sách chính sách. Muốn biết một bảng đang "
    "được bảo vệ thế nào thì phải xem CẢ HAI bảng dữ liệu bên dưới."
)

LEGACY_IS_SQL_ONLY = (
    "Không thể tạo, sửa hay gỡ row filter / column mask kiểu cũ qua API hay SDK — "
    "Databricks chỉ hỗ trợ bằng SQL trên một SQL Warehouse. Ứng dụng này chỉ HIỂN THỊ "
    "chúng và cố ý không dựng nút bấm giả. Người có quyền MODIFY trên bảng và EXECUTE "
    "trên hàm cần tự chạy câu lệnh tương ứng trong Databricks SQL."
)

LEGACY_SQL_SHAPES = (
    "Dạng câu lệnh (chạy thủ công trong Databricks SQL, ứng dụng không gửi giúp):\n"
    "  ALTER TABLE <bảng> SET ROW FILTER <hàm> ON (<cột>, …);\n"
    "  ALTER TABLE <bảng> DROP ROW FILTER;\n"
    "  ALTER TABLE <bảng> ALTER COLUMN <cột> SET MASK <hàm>;\n"
    "  ALTER TABLE <bảng> ALTER COLUMN <cột> DROP MASK;"
)

NO_SIMULATION = (
    "Databricks KHÔNG có API chạy thử (dry-run) cho chính sách ABAC và KHÔNG có API "
    "trả về danh sách cột mà một alias thực sự khớp. Mọi con số trong bản xem trước "
    "chỉ là nội dung yêu cầu sắp gửi đi — ứng dụng không mô phỏng và không được coi "
    "bất kỳ kết quả nào là điều engine Databricks sẽ làm. Cách duy nhất để biết chắc "
    "là truy vấn thật bằng một danh tính thật sau khi áp dụng."
)

WHEN_CONDITION_DEFAULT_TRUE = (
    "`when_condition` là điều kiện chọn BẢNG theo governed tag "
    "(`has_tag('k')`, `has_tag_value('k','v')`, kết hợp AND / OR / NOT), KHÔNG phải "
    "điều kiện lọc dòng. **Để trống nghĩa là TRUE**: chính sách áp dụng cho MỌI bảng "
    "trong phạm vi đã gắn."
)

MATCH_COLUMNS_RULES = (
    "`match_columns` chọn cột theo thẻ ở cấp CỘT: tối đa 3 mục và TẤT CẢ phải khớp "
    "(AND, không phải OR). Nó chỉ nhìn thấy thẻ gán TRỰC TIẾP lên cột — thẻ kế thừa "
    "từ catalog, schema hay bảng đều không tính. `column_mask.on_column` trỏ tới "
    "ALIAS trong `match_columns`, không phải tên cột vật lý. Nếu một alias khớp nhiều "
    "hơn một cột trong cùng một bảng, Databricks coi đó là xung đột và CHẶN truy vấn."
)

FUNCTION_ARGUMENT_RULES = (
    "Với column mask, tham số ĐẦU TIÊN của UDF là giá trị cột bị che và được truyền "
    "ngầm — `using` chỉ khai báo các tham số BỔ SUNG. Với row filter, TẤT CẢ tham số "
    "đều lấy từ `using`. `constant` luôn là chuỗi, kể cả khi tham số của UDF kiểu INT."
)

CONFLICT_FAIL_CLOSED = (
    "Mỗi cặp (bảng, người dùng) chỉ được phép quy ra ĐÚNG MỘT row filter, và mỗi cặp "
    "(cột, người dùng) đúng MỘT column mask. Databricks so sánh theo HÀM, không theo "
    "dữ liệu: hai hàm khác nhau cùng áp vào là truy vấn bị CHẶN (fail-closed), kể cả "
    "khi một bên là chính sách ABAC còn bên kia là ràng buộc legacy gắn sẵn trên bảng. "
    "Hãy kiểm tra cả hai cơ chế trước khi tạo chính sách ở cấp catalog."
)

UPDATE_MASK_RULES = (
    "`update_mask` là field mask thật sự: để trống hoặc đặt `*` sẽ áp dụng TOÀN BỘ "
    "các trường của đối tượng gửi lên và XOÁ những trường không được điền. Ứng dụng "
    "luôn dựng mask liệt kê rõ từng trường. Đây cũng là cách duy nhất để gỡ "
    "`except_principals` hoặc `when_condition`: đưa tên trường vào mask và gửi giá "
    "trị rỗng."
)

RENAME_NOT_OFFERED = (
    "Ứng dụng không đổi tên chính sách: ngữ nghĩa của field mask cho trường `name` "
    "không được Databricks ghi rõ. Muốn đổi tên thì xoá rồi tạo lại."
)

GOVERNED_TAGS_REQUIRED = (
    "Chính sách ABAC chỉ hoạt động với GOVERNED TAG (thẻ đã khai báo trong tag "
    "policy), không phải thẻ tự do thông thường. Thẻ chưa được quản trị sẽ không bao "
    "giờ khớp."
)

DELETE_SIDE_EFFECTS = (
    "Xoá governed tag mà chính sách đang tham chiếu, hoặc xoá UDF mà chính sách đang "
    "gọi, KHÔNG làm lệnh xoá thất bại — nhưng các TRUY VẤN sau đó sẽ hỏng với "
    "UC_ABAC_UNKNOWN_TAG_POLICY hoặc UC_DEPENDENCY_DOES_NOT_EXIST. Hãy gỡ chính sách "
    "trước khi xoá thẻ hoặc hàm."
)

PIPELINE_OWNER_RISK = (
    "Materialized view và streaming table đánh giá chính sách theo danh tính CHỦ SỞ "
    "HỮU PIPELINE, nên dữ liệu đã bị che có thể bị ghi cứng vào kết quả vật chất hoá. "
    "Cách xử lý được Databricks khuyến nghị là thêm danh tính chạy pipeline vào "
    "`except_principals`."
)

TIME_TRAVEL_BLOCKED = (
    "Bảng đang có row filter hoặc column mask hiệu lực thì truy vấn time-travel "
    "(VERSION AS OF / TIMESTAMP AS OF) sẽ THẤT BẠI, và thao tác clone không được hỗ trợ."
)

VIEW_NOT_ATTACHABLE = (
    "Không gắn được chính sách vào VIEW. `for_securable_type` luôn là TABLE, trong đó "
    "TABLE bao gồm cả streaming table và materialized view."
)

PERMISSIONS_NOTE = (
    "Đọc chính sách cần READ METADATA, MANAGE hoặc quyền sở hữu trên điểm gắn. "
    "Ghi chính sách cần MANAGE hoặc quyền sở hữu trên điểm gắn, CỘNG THÊM quyền "
    "EXECUTE trên UDF được chính sách gọi."
)

#: Shown under every policy listing.
LISTING_CAVEAT = (
    "Danh sách này chỉ gồm chính sách ABAC mà danh tính thực thi được phép nhìn thấy. "
    "Nó không bao gồm row filter / column mask gắn trực tiếp trên bảng theo kiểu cũ."
)


@dataclass(frozen=True)
class AttachPoint:
    """The (securable_type, fullname) pair the policies API is addressed by."""

    securable_type: str
    full_name: str
    kind: str

    @property
    def cascades(self) -> bool:
        return self.kind in CASCADING_KINDS

    @property
    def quota(self) -> int:
        return QUOTAS.get(self.kind, 0)


@dataclass
class PolicyRow:
    """One policy, flattened for display and for state fingerprinting."""

    name: str
    policy_type: str
    attach_type: str
    attach_fullname: str
    to_principals: list[str] = field(default_factory=list)
    except_principals: list[str] = field(default_factory=list)
    when_condition: str = ""
    match_columns: list[dict] = field(default_factory=list)
    function_name: str = ""
    on_column: str = ""
    using: list[dict] = field(default_factory=list)
    comment: str = ""
    policy_id: str = ""
    created_at: str = ""
    created_by: str = ""
    updated_at: str = ""
    updated_by: str = ""
    #: True when the policy is attached to an ancestor, not to the object the
    #: operator is looking at. Such a policy cannot be edited from here.
    inherited: bool = False

    @property
    def type_label(self) -> str:
        return POLICY_TYPE_LABELS.get(self.policy_type, self.policy_type or "Chưa xác định")

    @property
    def is_column_mask(self) -> bool:
        return self.policy_type == COLUMN_MASK

    @property
    def applies_to_every_table(self) -> bool:
        """An absent ``when_condition`` defaults to TRUE, not to "nothing"."""
        return not self.when_condition.strip()

    @property
    def scope_label(self) -> str:
        where = f"{self.attach_type} {self.attach_fullname}".strip()
        return f"Kế thừa từ {where}" if self.inherited else f"Gắn tại {where}"

    @property
    def editable_here(self) -> bool:
        """Only a policy attached to this very object can be changed here."""
        return not self.inherited

    def aliases(self) -> list[str]:
        return [str(m.get("alias") or "") for m in self.match_columns if m.get("alias")]

    def row(self) -> dict:
        return {
            "Tên chính sách": self.name,
            "Loại": self.type_label,
            "Phạm vi": self.scope_label,
            "Áp dụng cho": ", ".join(self.to_principals) or "—",
            "Loại trừ": ", ".join(self.except_principals) or "—",
            "Chọn bảng": self.when_condition or "(trống = TRUE: mọi bảng trong phạm vi)",
            "Chọn cột": " AND ".join(
                f"{m.get('alias') or '?'}: {m.get('condition') or '?'}"
                for m in self.match_columns
            ) or "—",
            "Hàm": self.function_name or "—",
            "Trên alias": self.on_column or "—",
            "Sửa được tại đây": "Có" if self.editable_here else "Không",
            "Cập nhật": self.updated_at or self.created_at or "—",
        }

    def state(self) -> dict:
        """The substance of the policy, for fingerprinting and verification.

        Timestamps and ``updated_by`` are excluded: they move on their own and
        would make every plan look stale.
        """
        return {
            "name": self.name,
            "policy_type": self.policy_type,
            "attach": f"{self.attach_type}:{self.attach_fullname}",
            "to_principals": sorted(self.to_principals),
            "except_principals": sorted(self.except_principals),
            "when_condition": self.when_condition,
            "match_columns": self.match_columns,
            "function_name": self.function_name,
            "on_column": self.on_column,
            "using": self.using,
            "comment": self.comment,
        }


@dataclass
class PolicyDraft:
    """What an operator wants a policy to say. Validated before it is used."""

    name: str = ""
    policy_type: str = ROW_FILTER
    to_principals: list[str] = field(default_factory=list)
    except_principals: list[str] = field(default_factory=list)
    #: Governed-tag expression selecting TABLES. Empty means TRUE.
    when_condition: str = ""
    #: ``[{"alias": "...", "condition": "hasTagValue('pii','email')"}]``
    match_columns: list[dict] = field(default_factory=list)
    function_name: str = ""
    #: For a column mask: the ``match_columns`` alias the UDF is applied to.
    on_column: str = ""
    #: ``[{"alias": "..."}]`` or ``[{"constant": "..."}]`` - one key per entry.
    using: list[dict] = field(default_factory=list)
    comment: str = ""

    @property
    def is_column_mask(self) -> bool:
        return self.policy_type == COLUMN_MASK


@dataclass
class LegacyProtections:
    """Table-attached row filter / column masks, read from an AssetDetail."""

    target: Target | None = None
    row_filter: dict | None = None
    column_masks: dict = field(default_factory=dict)
    observed_at: str = ""

    @property
    def any_present(self) -> bool:
        return bool(self.row_filter) or bool(self.column_masks)

    def rows(self) -> list[dict]:
        out: list[dict] = []
        if self.row_filter:
            data = as_dict(self.row_filter)
            out.append({
                "Cơ chế": "Row filter (legacy, gắn trên bảng)",
                "Đối tượng": self.target.full_name if self.target else "—",
                "Hàm": data.get("function_name") or data.get("name") or "—",
                "Tham số": ", ".join(str(x) for x in (data.get("input_column_names") or [])) or "—",
            })
        for column, mask in sorted(self.column_masks.items()):
            data = as_dict(mask)
            out.append({
                "Cơ chế": "Column mask (legacy, gắn trên bảng)",
                "Đối tượng": f"{self.target.full_name}.{column}" if self.target else str(column),
                "Hàm": data.get("function_name") or data.get("name") or "—",
                "Tham số": ", ".join(str(x) for x in (data.get("using_column_names") or [])) or "—",
            })
        return out

    def note(self) -> str:
        if self.any_present:
            return LEGACY_IS_SQL_ONLY
        return (
            "Không có row filter / column mask kiểu cũ nào trên bảng này theo metadata "
            "mà danh tính thực thi đọc được. Đây là 'không có dữ liệu', không phải "
            "'không đủ quyền' — nếu việc đọc metadata thất bại thì màn hình tài sản đã "
            "báo lỗi riêng."
        )


class PolicyService(Service):
    """Reads and changes ABAC policies; explains everything it cannot do."""

    capability_keys = ("policies.read", "policies.write", "masks.legacy")

    # -- attach points ----------------------------------------------------
    def attach_point(self, target: Target, metastore_id: str = "") -> AttachPoint:
        """Turn a target into the pair ``list_policies`` is addressed by.

        The attach point is *where the policy hangs*, not what it protects: a
        policy on a catalog cascades to every table beneath it.
        """
        if target.kind == "metastore":
            # A metastore has no dotted name, so the caller must supply the id.
            resolved = (metastore_id or "").strip()
            if not resolved:
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    "Gắn chính sách ở cấp metastore cần metastore_id; tên metastore "
                    "không phải là tên Unity Catalog có dấu chấm.",
                )
            return AttachPoint("METASTORE", resolved, "metastore")
        if target.kind not in ATTACH_KINDS:
            raise GovernanceError(
                Code.CAPABILITY_UNAVAILABLE,
                "Chỉ gắn được chính sách ABAC vào catalog, schema hoặc bảng. "
                + VIEW_NOT_ATTACHABLE,
            )
        return AttachPoint(target.securable_type, target.full_name, target.kind)

    def metastore_attach(self, metastore_id: str = "") -> dict:
        """Probe whether this workspace accepts a METASTORE-level attach point.

        Metastore scope is Beta, so it is never assumed. The probe is read-only
        and distinguishes the three answers that matter: supported, not offered
        by this workspace, and "cannot tell because the identity lacks the
        privilege" - a missing privilege is not evidence the feature is absent.
        """
        resolved = (metastore_id or "").strip()
        if not resolved:
            try:
                summary = self.w.metastores.summary()
                resolved = str(getattr(summary, "metastore_id", "") or "")
            except Exception:
                resolved = ""
        if not resolved:
            return {
                "supported": None,
                "metastore_id": "",
                "note": "Chưa xác định được metastore của workspace này, nên chưa thể "
                        "kiểm tra phạm vi metastore.",
            }
        try:
            take(self.w.policies.list_policies(
                on_securable_type="METASTORE",
                on_securable_fullname=resolved,
                include_inherited=False,
            ), 1)
        except Exception as exc:
            err = translate(exc)
            if err.code == Code.CAPABILITY_UNAVAILABLE:
                return {
                    "supported": False,
                    "metastore_id": resolved,
                    "note": "Workspace này chưa mở phạm vi metastore (tính năng Beta) "
                            "cho chính sách ABAC.",
                }
            if err.code == Code.PERMISSION_DENIED:
                return {
                    "supported": None,
                    "metastore_id": resolved,
                    "note": "Không đủ quyền để kiểm tra phạm vi metastore. Đây là vấn đề "
                            "quyền, KHÔNG phải bằng chứng tính năng không tồn tại.",
                }
            return {
                "supported": None,
                "metastore_id": resolved,
                "note": f"Chưa xác định được phạm vi metastore ({err.code}).",
            }
        return {
            "supported": True,
            "metastore_id": resolved,
            "note": "Phạm vi metastore khả dụng (Beta). Chính sách gắn ở đây lan xuống "
                    "toàn bộ catalog trong metastore.",
        }

    def quota_note(self, attach: AttachPoint, used: int | None = None) -> str:
        limit = attach.quota
        if not limit:
            return ""
        if used is None:
            return f"Hạn mức: tối đa {limit} chính sách tại một {attach.kind}."
        return f"Hạn mức: đang dùng {used}/{limit} chính sách tại {attach.kind} này."

    # -- reading ----------------------------------------------------------
    def list_for(
        self,
        target: Target,
        include_inherited: bool = True,
        *,
        metastore_id: str = "",
        limit: int = PAGE_LIMIT,
    ) -> Listing:
        """Policies visible at this attach point, optionally with inherited ones.

        ``include_inherited=True`` adds policies attached higher up that reach
        this object; they are marked and are not editable from here.
        """
        def run() -> Listing:
            self.authz.require(Action.READ_POLICIES, target)
            attach = self.attach_point(target, metastore_id)
            items, more = take(
                self.w.policies.list_policies(
                    on_securable_type=attach.securable_type,
                    on_securable_fullname=attach.full_name,
                    include_inherited=include_inherited,
                ),
                limit,
            )
            rows = [_to_row(item, attach) for item in items]
            rows.sort(key=lambda r: (r.inherited, r.name.lower()))
            note = LISTING_CAVEAT
            if not rows:
                note = (
                    "Không có chính sách ABAC nào tại phạm vi này"
                    + (" (đã tính cả chính sách kế thừa)." if include_inherited else ".")
                    + " Đây là 'không có dữ liệu'; nếu thiếu quyền đọc thì panel sẽ báo lỗi "
                    "quyền thay vì danh sách rỗng. " + LISTING_CAVEAT
                )
            return Listing(
                items=rows,
                completeness=Completeness.TRUNCATED if more else Completeness.COMPLETE,
                observed_at=now_text(),
                note=note,
            )

        return self.listing(run)

    def get(self, target: Target, name: str, *, metastore_id: str = "") -> PolicyRow:
        """One policy at this attach point."""
        self.authz.require(Action.READ_POLICIES, target)
        attach = self.attach_point(target, metastore_id)
        policy_name = validate_component(name, "Tên chính sách")
        info = self.read(lambda: self.w.policies.get_policy(
            on_securable_type=attach.securable_type,
            on_securable_fullname=attach.full_name,
            name=policy_name,
        ))
        return _to_row(info, attach)

    def legacy_protections(self, detail: "AssetDetail") -> LegacyProtections:
        """The *other* mechanism: row filter / column masks bound to the table.

        These come from ``TableInfo``/``ColumnInfo`` and are never ABAC
        policies. They are read-only here on purpose - see
        :data:`LEGACY_IS_SQL_ONLY`.
        """
        return LegacyProtections(
            target=getattr(detail, "target", None),
            row_filter=getattr(detail, "row_filter", None) or None,
            column_masks=dict(getattr(detail, "column_masks", None) or {}),
            observed_at=now_text(),
        )

    # -- validation -------------------------------------------------------
    def validate_draft(
        self,
        draft: PolicyDraft,
        attach: AttachPoint,
        *,
        existing: PolicyRow | None = None,
        fields: Sequence[str] | None = None,
    ) -> PolicyDraft:
        """Return a cleaned copy, or raise with a Vietnamese explanation.

        ``fields`` is the update field list: when a field is not being updated,
        the corresponding rules are checked against ``existing`` instead of the
        draft, so an update that touches only the principals is not forced to
        restate the whole mask definition.
        """
        changing = set(fields) if fields is not None else set(UPDATABLE_FIELDS)

        policy_type = (draft.policy_type or "").strip().upper()
        if existing is not None:
            # Changing a policy's type in place is not a documented operation.
            if policy_type and policy_type != existing.policy_type:
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    "Không đổi được loại chính sách sau khi đã tạo. Hãy xoá rồi tạo lại.",
                )
            policy_type = existing.policy_type
        if policy_type not in (ROW_FILTER, COLUMN_MASK):
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Loại chính sách phải là row filter hoặc column mask.",
            )

        name = validate_component(draft.name or (existing.name if existing else ""),
                                  "Tên chính sách")

        to_principals = _principals(draft.to_principals, "Danh sách áp dụng")
        except_principals = _principals(draft.except_principals, "Danh sách loại trừ")
        if existing is not None and "to_principals" not in changing:
            to_principals = list(existing.to_principals)
        if existing is not None and "except_principals" not in changing:
            except_principals = list(existing.except_principals)

        if not to_principals:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Cần ít nhất một principal trong danh sách áp dụng. Dùng `account users` "
                "nếu thực sự muốn áp cho tất cả.",
            )
        overlap = sorted(set(p.lower() for p in to_principals)
                         & set(p.lower() for p in except_principals))
        if overlap:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Một principal không thể vừa được áp dụng vừa được loại trừ: "
                + ", ".join(overlap) + ".",
            )
        total = len(to_principals) + len(except_principals)
        if total > MAX_PRINCIPALS:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Tổng số principal (áp dụng + loại trừ) tối đa {MAX_PRINCIPALS}, "
                f"đang có {total}.",
            )

        when_condition = (draft.when_condition or "").strip()
        if existing is not None and "when_condition" not in changing:
            when_condition = existing.when_condition
        if len(when_condition) > 4000:
            raise GovernanceError(Code.INVALID_INPUT, "Điều kiện chọn bảng quá dài.")

        match_columns = _match_columns(draft.match_columns)
        if existing is not None and "match_columns" not in changing:
            match_columns = list(existing.match_columns)

        comment = (draft.comment or "").strip()
        if existing is not None and "comment" not in changing:
            comment = existing.comment
        if len(comment) > 1000:
            raise GovernanceError(Code.INVALID_INPUT, "Mô tả tối đa 1000 ký tự.")

        definition_field = "column_mask" if policy_type == COLUMN_MASK else "row_filter"
        touching_definition = existing is None or definition_field in changing

        function_name = (draft.function_name or "").strip()
        on_column = (draft.on_column or "").strip()
        using = _function_arguments(draft.using)
        if not touching_definition and existing is not None:
            function_name = existing.function_name
            on_column = existing.on_column
            using = list(existing.using)
        else:
            if not function_name:
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    "Cần tên đầy đủ của UDF (catalog.schema.tên_hàm).",
                )
            # A UDF is a three-part Unity Catalog name; parsing validates every
            # component and rejects anything unquotable.
            Target.parse("function", function_name)

        if policy_type == COLUMN_MASK:
            aliases = [str(m["alias"]) for m in match_columns]
            if not aliases:
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    "Column mask cần ít nhất một mục `match_columns` để chỉ ra cột nào "
                    "bị che. " + MATCH_COLUMNS_RULES,
                )
            if not on_column:
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    "Thiếu `on_column`: hãy chọn một ALIAS trong `match_columns`, "
                    "không phải tên cột vật lý.",
                )
            if on_column not in aliases:
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    f"`on_column` = '{on_column}' không có trong các alias đã khai báo "
                    f"({', '.join(aliases)}).",
                )
        else:
            if on_column:
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    "Row filter không dùng `on_column`; nó áp cho cả dòng chứ không cho "
                    "một cột.",
                )
            # Row-filter arguments may reference aliases, so those aliases must exist.
            declared = {str(m["alias"]) for m in match_columns}
            unknown = [a["alias"] for a in using if a.get("alias") and a["alias"] not in declared]
            if unknown:
                raise GovernanceError(
                    Code.INVALID_INPUT,
                    "Tham số tham chiếu alias chưa khai báo trong `match_columns`: "
                    + ", ".join(sorted(unknown)) + ".",
                )

        if attach.kind == "metastore" and existing is None:
            probe = self.metastore_attach(attach.full_name)
            if probe["supported"] is False:
                raise GovernanceError(Code.CAPABILITY_UNAVAILABLE, probe["note"])

        return PolicyDraft(
            name=name,
            policy_type=policy_type,
            to_principals=to_principals,
            except_principals=except_principals,
            when_condition=when_condition,
            match_columns=match_columns,
            function_name=function_name,
            on_column=on_column,
            using=using,
            comment=comment,
        )

    def validate_table_attach(self, table_type: str) -> str:
        """Refuse a view, and flag a pipeline-materialised table.

        Returns a warning to add to the preview, or an empty string.
        """
        kind = (table_type or "").strip().upper()
        if kind == "VIEW":
            raise GovernanceError(Code.CAPABILITY_UNAVAILABLE, VIEW_NOT_ATTACHABLE)
        if kind in PIPELINE_MANAGED_TABLE_TYPES:
            return PIPELINE_OWNER_RISK
        return ""

    # -- planning ---------------------------------------------------------
    def plan_create(
        self,
        target: Target,
        draft: PolicyDraft,
        reason: str,
        *,
        metastore_id: str = "",
        table_type: str = "",
    ) -> Plan:
        """Validate -> Authorize -> Build plan -> Preview. Nothing is sent."""
        self.authz.require(Action.MANAGE_POLICY, target)
        attach = self.attach_point(target, metastore_id)
        reason = _reason(reason)
        pipeline_warning = self.validate_table_attach(table_type) if attach.kind == "table" else ""
        clean = self.validate_draft(draft, attach)

        existing = self._direct_rows(target, metastore_id)
        if any(r.name == clean.name for r in existing):
            raise GovernanceError(
                Code.CONFLICT,
                f"Đã có chính sách tên '{clean.name}' tại {attach.full_name}.",
            )
        if attach.quota and len(existing) >= attach.quota:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Đã đạt hạn mức {attach.quota} chính sách tại {attach.kind} này. "
                "Gộp hoặc xoá bớt chính sách cũ trước khi tạo mới.",
            )

        plan = self._plan(
            target, attach, "create", clean, reason,
            before=fingerprint(sorted(r.name for r in existing)),
            summary=f"Tạo chính sách {POLICY_TYPE_LABELS.get(clean.policy_type, '')} "
                    f"'{clean.name}'",
        )
        plan.preview = self._preview_create(attach, clean, len(existing), pipeline_warning)
        return plan

    def plan_update(
        self,
        target: Target,
        name: str,
        draft: PolicyDraft,
        fields: Sequence[str],
        reason: str,
        *,
        metastore_id: str = "",
    ) -> Plan:
        """Plan a field-masked update. ``fields`` is never allowed to be empty."""
        self.authz.require(Action.MANAGE_POLICY, target)
        attach = self.attach_point(target, metastore_id)
        reason = _reason(reason)

        chosen = [f for f in dict.fromkeys(fields or ()) if f]
        if not chosen:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Chọn ít nhất một trường cần cập nhật. Ứng dụng không gửi field mask "
                "rỗng vì Databricks sẽ hiểu là ghi đè toàn bộ. " + UPDATE_MASK_RULES,
            )
        unknown = [f for f in chosen if f not in UPDATABLE_FIELDS]
        if unknown:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Trường không cập nhật được: " + ", ".join(unknown) + ". " + RENAME_NOT_OFFERED,
            )

        existing = self.get(target, name, metastore_id=metastore_id)
        if existing.inherited:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Chính sách này được gắn tại {existing.attach_fullname}, không phải tại "
                f"{attach.full_name}. Hãy sửa ở đúng nơi nó được gắn.",
            )
        # A column mask is defined by `column_mask`; a row filter by `row_filter`.
        wrong = "row_filter" if existing.is_column_mask else "column_mask"
        if wrong in chosen:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Chính sách '{existing.name}' là {existing.type_label}; không có trường "
                f"`{wrong}` để cập nhật.",
            )

        clean = self.validate_draft(draft, attach, existing=existing, fields=chosen)
        after = _draft_state(clean, existing)
        if after == existing.state():
            raise GovernanceError(
                Code.ALREADY_SATISFIED,
                "Các giá trị gửi lên trùng với trạng thái hiện tại của chính sách.",
            )

        plan = self._plan(
            target, attach, "update", clean, reason,
            before=fingerprint(existing.state()),
            summary=f"Cập nhật chính sách '{existing.name}'",
            extra={"fields": chosen,
                   "update_mask": ",".join(UPDATABLE_FIELDS[f][0] for f in chosen)},
        )
        plan.preview = self._preview_update(attach, existing, clean, chosen)
        return plan

    def plan_delete(
        self,
        target: Target,
        name: str,
        reason: str,
        *,
        metastore_id: str = "",
    ) -> Plan:
        """Plan the removal of one policy."""
        self.authz.require(Action.MANAGE_POLICY, target)
        attach = self.attach_point(target, metastore_id)
        reason = _reason(reason)

        existing = self.get(target, name, metastore_id=metastore_id)
        if existing.inherited:
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"Chính sách này được gắn tại {existing.attach_fullname}. Xoá nó ở đây "
                "không có tác dụng; phải xoá tại đúng điểm gắn.",
            )

        draft = PolicyDraft(
            name=existing.name,
            policy_type=existing.policy_type,
            to_principals=list(existing.to_principals),
            except_principals=list(existing.except_principals),
            when_condition=existing.when_condition,
            match_columns=list(existing.match_columns),
            function_name=existing.function_name,
            on_column=existing.on_column,
            using=list(existing.using),
            comment=existing.comment,
        )
        plan = self._plan(
            target, attach, "delete", draft, reason,
            before=fingerprint(existing.state()),
            summary=f"Xoá chính sách '{existing.name}'",
        )
        plan.preview = self._preview_delete(attach, existing)
        return plan

    def _plan(
        self,
        target: Target,
        attach: AttachPoint,
        operation: str,
        draft: PolicyDraft,
        reason: str,
        *,
        before: str,
        summary: str,
        extra: dict | None = None,
    ) -> Plan:
        payload: dict[str, Any] = {
            "operation": operation,
            "attach_type": attach.securable_type,
            "attach_fullname": attach.full_name,
            "name": draft.name,
            "policy_type": draft.policy_type,
            "to_principals": list(draft.to_principals),
            "except_principals": list(draft.except_principals),
            "when_condition": draft.when_condition,
            "match_columns": list(draft.match_columns),
            "function_name": draft.function_name,
            "on_column": draft.on_column,
            "using": list(draft.using),
            "comment": draft.comment,
        }
        payload.update(extra or {})
        return Plan(
            action=Action.MANAGE_POLICY,
            target_key=target.key,
            target_name=attach.full_name,
            target_type=target.label,
            actor=self.ctx.actor.email,
            execution_identity=self.ctx.execution_identity,
            payload=payload,
            before=before,
            reason=reason,
            summary=summary,
            # One policy call touches one policy, but that policy can cascade
            # over many tables, so the blast radius is never atomic in practice.
            multi_object=attach.cascades,
        )

    # -- previews ---------------------------------------------------------
    def _common_lines(self, draft: PolicyDraft) -> list[PreviewLine]:
        lines = [
            PreviewLine(
                "Yêu cầu sẽ được gửi bằng: "
                + ("tài khoản dịch vụ của ứng dụng"
                   if self.ctx.execution_identity == "app_service_principal"
                   else "hồ sơ đăng nhập hiện tại"),
                "info",
            ),
            PreviewLine(PERMISSIONS_NOTE, "info"),
            PreviewLine(NO_SIMULATION, "unknown"),
        ]
        if draft.policy_type == COLUMN_MASK:
            lines.append(PreviewLine(MATCH_COLUMNS_RULES, "warning"))
        lines.append(PreviewLine(FUNCTION_ARGUMENT_RULES, "info"))
        return lines

    def _preview_create(
        self,
        attach: AttachPoint,
        draft: PolicyDraft,
        used: int,
        pipeline_warning: str,
    ) -> list[PreviewLine]:
        label = POLICY_TYPE_LABELS.get(draft.policy_type, draft.policy_type)
        lines = [
            PreviewLine(f"Tạo chính sách '{draft.name}' — {label}.", "change"),
            PreviewLine(
                f"Gắn tại: {attach.securable_type} {attach.full_name}. "
                "Chính sách sẽ lan xuống MỌI bảng con trong phạm vi này."
                if attach.cascades else
                f"Gắn tại: {attach.securable_type} {attach.full_name}.",
                "change",
            ),
            PreviewLine("Áp dụng cho: " + ", ".join(draft.to_principals), "change"),
        ]
        if draft.except_principals:
            lines.append(PreviewLine(
                "Loại trừ (không bị che): " + ", ".join(draft.except_principals), "change"))
        if draft.policy_type == COLUMN_MASK:
            lines.append(PreviewLine(
                f"Hàm che: {draft.function_name}, áp lên alias '{draft.on_column}'.", "change"))
        else:
            lines.append(PreviewLine(f"Hàm lọc dòng: {draft.function_name}.", "change"))
        if draft.match_columns:
            lines.append(PreviewLine(
                "Chọn cột (tất cả điều kiện phải cùng đúng): "
                + " AND ".join(f"{m['alias']} khi {m['condition']}" for m in draft.match_columns),
                "change",
            ))

        # The loudest line in the whole module: an empty when_condition is TRUE.
        if not draft.when_condition:
            severity = "warning"
            text = WHEN_CONDITION_DEFAULT_TRUE
            if attach.cascades:
                text = (
                    "CẢNH BÁO PHẠM VI: `when_condition` đang để trống trong khi chính sách "
                    f"được gắn ở cấp {attach.kind}. Chính sách sẽ áp cho MỌI BẢNG thuộc "
                    f"{attach.full_name}, kể cả bảng tạo về sau. " + WHEN_CONDITION_DEFAULT_TRUE
                )
            lines.append(PreviewLine(text, severity))
        else:
            lines.append(PreviewLine(
                f"Chỉ áp cho bảng thoả: {draft.when_condition}. " + WHEN_CONDITION_DEFAULT_TRUE,
                "info",
            ))

        lines.append(PreviewLine(
            "Số bảng và số cột thực sự bị ảnh hưởng: Chưa xác định — Databricks không "
            "cung cấp phép đếm này và ứng dụng không ước lượng.",
            "unknown",
        ))
        lines.append(PreviewLine(GOVERNED_TAGS_REQUIRED, "warning"))
        lines.append(PreviewLine(CONFLICT_FAIL_CLOSED, "warning"))
        lines.append(PreviewLine(TIME_TRAVEL_BLOCKED, "warning"))
        lines.append(PreviewLine(VIEW_NOT_ATTACHABLE, "info"))
        if pipeline_warning:
            lines.append(PreviewLine(pipeline_warning, "warning"))
        elif attach.cascades:
            lines.append(PreviewLine(PIPELINE_OWNER_RISK, "warning"))
        note = self.quota_note(attach, used)
        if note:
            lines.append(PreviewLine(note, "info"))
        lines.append(PreviewLine(TWO_MECHANISMS, "info"))
        lines.extend(self._common_lines(draft))
        return lines

    def _preview_update(
        self,
        attach: AttachPoint,
        existing: PolicyRow,
        draft: PolicyDraft,
        fields: Sequence[str],
    ) -> list[PreviewLine]:
        mask = ",".join(UPDATABLE_FIELDS[f][0] for f in fields)
        lines = [
            PreviewLine(
                f"Cập nhật chính sách '{existing.name}' ({existing.type_label}) tại "
                f"{attach.securable_type} {attach.full_name}.",
                "change",
            ),
            PreviewLine(
                "Chỉ các trường sau được gửi đi: "
                + ", ".join(UPDATABLE_FIELDS[f][1] for f in fields)
                + f" (update_mask = {mask}). Các trường khác giữ nguyên.",
                "change",
            ),
            PreviewLine(UPDATE_MASK_RULES, "info"),
        ]
        for f in fields:
            before, after = _field_texts(f, existing, draft)
            lines.append(PreviewLine(
                f"{UPDATABLE_FIELDS[f][1]}: “{before}” → “{after}”.", "change"))
        if "when_condition" in fields and not draft.when_condition:
            text = WHEN_CONDITION_DEFAULT_TRUE
            if attach.cascades:
                text = (
                    "CẢNH BÁO PHẠM VI: đang GỠ `when_condition` khỏi một chính sách gắn ở "
                    f"cấp {attach.kind}. Sau khi cập nhật, chính sách sẽ áp cho MỌI BẢNG "
                    f"thuộc {attach.full_name}. " + WHEN_CONDITION_DEFAULT_TRUE
                )
            lines.append(PreviewLine(text, "warning"))
        if "except_principals" in fields and not draft.except_principals and existing.except_principals:
            lines.append(PreviewLine(
                "Đang GỠ toàn bộ danh sách loại trừ: những principal này sẽ bắt đầu bị "
                "che dữ liệu. Nếu trong đó có danh tính chạy pipeline, hãy đọc kỹ cảnh "
                "báo về materialized view / streaming table.",
                "warning",
            ))
        if "to_principals" in fields:
            lines.append(PreviewLine(
                "Mở rộng hay thu hẹp danh sách áp dụng đều đổi kết quả truy vấn ngay ở "
                "lần chạy kế tiếp, không có bước triển khai riêng.",
                "warning",
            ))
        lines.append(PreviewLine(CONFLICT_FAIL_CLOSED, "warning"))
        lines.append(PreviewLine(
            "Số bảng và số cột thực sự bị ảnh hưởng: Chưa xác định.", "unknown"))
        lines.extend(self._common_lines(draft))
        return lines

    def _preview_delete(self, attach: AttachPoint, existing: PolicyRow) -> list[PreviewLine]:
        lines = [
            PreviewLine(
                f"Xoá chính sách '{existing.name}' ({existing.type_label}) tại "
                f"{attach.securable_type} {attach.full_name}.",
                "change",
            ),
            PreviewLine(
                "Sau khi xoá, dữ liệu trước đây bị che sẽ hiển thị NGUYÊN BẢN cho "
                + ", ".join(existing.to_principals)
                + " ở lần truy vấn kế tiếp. Không có bước hoàn tác.",
                "warning",
            ),
        ]
        if existing.applies_to_every_table and attach.cascades:
            lines.append(PreviewLine(
                f"Chính sách này đang áp cho MỌI bảng thuộc {attach.full_name} "
                "(`when_condition` trống = TRUE), nên phạm vi mất bảo vệ là toàn bộ "
                "phạm vi đó.",
                "warning",
            ))
        lines.append(PreviewLine(
            f"Hàm {existing.function_name or '—'} và các governed tag liên quan KHÔNG bị "
            "xoá theo.",
            "info",
        ))
        lines.append(PreviewLine(DELETE_SIDE_EFFECTS, "warning"))
        lines.append(PreviewLine(
            "Nếu bảng vẫn còn row filter / column mask kiểu cũ gắn trực tiếp, dữ liệu vẫn "
            "có thể bị che sau khi xoá chính sách này. " + TWO_MECHANISMS,
            "info",
        ))
        lines.append(PreviewLine(
            "Số bảng mất bảo vệ: Chưa xác định — Databricks không cung cấp phép đếm này.",
            "unknown",
        ))
        lines.append(PreviewLine(NO_SIMULATION, "unknown"))
        lines.append(PreviewLine(PERMISSIONS_NOTE, "info"))
        return lines

    # -- executing --------------------------------------------------------
    def apply(
        self,
        plan: Plan,
        target: Target,
        confirmation: str,
        *,
        metastore_id: str = "",
    ) -> Outcome:
        """The single mutation path for create, update and delete."""
        plan.require_confirmation(confirmation)
        attach = self.attach_point(target, metastore_id)
        if (attach.securable_type != plan.payload.get("attach_type")
                or attach.full_name != plan.payload.get("attach_fullname")):
            raise GovernanceError(
                Code.STALE_PLAN,
                "Bản xem trước thuộc về một điểm gắn khác. Hãy tạo lại bản xem trước.",
            )

        operation = plan.payload.get("operation")
        name = str(plan.payload.get("name") or "")
        update_mask = str(plan.payload.get("update_mask") or "").strip()
        if update_mask in ("", "*") and operation == "update":
            # A last line of defence: this plan could only have been built by
            # plan_update, but a wildcard mask silently destroys fields.
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Từ chối gửi field mask rỗng hoặc '*'. " + UPDATE_MASK_RULES,
            )

        if operation == "create":
            def revalidate() -> str:
                return fingerprint(
                    sorted(r.name for r in self._direct_rows(target, metastore_id)))

            def do():
                self.w.policies.create_policy(policy_info=self._policy_info(plan, attach))

            def verify() -> dict:
                return self.get(target, name, metastore_id=metastore_id).state()

        elif operation == "update":
            def revalidate() -> str:
                return fingerprint(self.get(target, name, metastore_id=metastore_id).state())

            def do():
                # The mask is built field by field; an empty or '*' mask would
                # wipe every field the request did not populate.
                self.w.policies.update_policy(
                    on_securable_type=attach.securable_type,
                    on_securable_fullname=attach.full_name,
                    name=name,
                    policy_info=self._policy_info(plan, attach),
                    update_mask=update_mask,
                )

            def verify() -> dict:
                return self.get(target, name, metastore_id=metastore_id).state()

        elif operation == "delete":
            def revalidate() -> str:
                return fingerprint(self.get(target, name, metastore_id=metastore_id).state())

            def do():
                self.w.policies.delete_policy(
                    on_securable_type=attach.securable_type,
                    on_securable_fullname=attach.full_name,
                    name=name,
                )

            def verify() -> dict:
                # A successful delete makes the read fail; anything else means
                # the policy is still there and the operator must be told.
                try:
                    self.get(target, name, metastore_id=metastore_id)
                except GovernanceError as err:
                    if err.code == Code.NOT_FOUND:
                        return {"name": name, "deleted": True}
                    raise
                return {
                    "name": name,
                    "deleted": False,
                    "note": "Databricks chấp nhận lệnh xoá nhưng chính sách vẫn đọc được. "
                            "Hãy làm mới và kiểm tra lại trước khi thử lần nữa.",
                }

        else:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Bản xem trước không mô tả một thao tác chính sách hợp lệ.",
            )

        return self.execute(
            plan, target, do,
            revalidate=revalidate, verify=verify,
            action_label=plan.summary,
        )

    def _policy_info(self, plan: Plan, attach: AttachPoint):
        """Build the ``PolicyInfo`` body from the already-validated plan."""
        from databricks.sdk.service.catalog import (
            ColumnMaskOptions, FunctionArgument, MatchColumn, PolicyInfo,
            PolicyType, RowFilterOptions, SecurableType,
        )

        payload = plan.payload
        policy_type = (
            PolicyType.POLICY_TYPE_COLUMN_MASK
            if payload.get("policy_type") == COLUMN_MASK
            else PolicyType.POLICY_TYPE_ROW_FILTER
        )
        arguments = [
            FunctionArgument(
                alias=a.get("alias") or None,
                # Always a string on the wire, even for an INT parameter.
                constant=None if a.get("constant") is None else str(a.get("constant")),
            )
            for a in (payload.get("using") or [])
        ] or None
        matchers = [
            MatchColumn(alias=m.get("alias") or None, condition=m.get("condition") or None)
            for m in (payload.get("match_columns") or [])
        ] or None

        column_mask = None
        row_filter = None
        if payload.get("policy_type") == COLUMN_MASK:
            column_mask = ColumnMaskOptions(
                function_name=str(payload.get("function_name") or ""),
                # This is a match_columns ALIAS, not a physical column name.
                on_column=str(payload.get("on_column") or ""),
                using=arguments,
            )
        else:
            row_filter = RowFilterOptions(
                function_name=str(payload.get("function_name") or ""),
                using=arguments,
            )

        return PolicyInfo(
            name=str(payload.get("name") or ""),
            policy_type=policy_type,
            # Both policy types protect TABLEs; there is no other legal value.
            for_securable_type=SecurableType.TABLE,
            on_securable_type=attach.securable_type,
            on_securable_fullname=attach.full_name,
            to_principals=list(payload.get("to_principals") or []),
            except_principals=list(payload.get("except_principals") or []) or None,
            when_condition=str(payload.get("when_condition") or "") or None,
            match_columns=matchers,
            column_mask=column_mask,
            row_filter=row_filter,
            comment=str(payload.get("comment") or "") or None,
        )

    # -- internals --------------------------------------------------------
    def _direct_rows(self, target: Target, metastore_id: str) -> list[PolicyRow]:
        """Policies attached to this very object. Raises rather than hiding."""
        listing = self.list_for(target, include_inherited=False, metastore_id=metastore_id)
        if not listing.ok:
            raise listing.error
        return [r for r in listing.items if not r.inherited]


def _to_row(info: Any, attach: AttachPoint) -> PolicyRow:
    data = as_dict(info)
    column_mask = as_dict(data.get("column_mask"))
    row_filter = as_dict(data.get("row_filter"))
    definition = column_mask or row_filter
    attached_type = enum_value(data.get("on_securable_type")) or attach.securable_type
    attached_name = str(data.get("on_securable_fullname") or attach.full_name)
    return PolicyRow(
        name=str(data.get("name") or ""),
        policy_type=enum_value(data.get("policy_type")),
        attach_type=attached_type,
        attach_fullname=attached_name,
        to_principals=[str(p) for p in (data.get("to_principals") or [])],
        except_principals=[str(p) for p in (data.get("except_principals") or [])],
        when_condition=str(data.get("when_condition") or ""),
        match_columns=[
            {"alias": str(as_dict(m).get("alias") or ""),
             "condition": str(as_dict(m).get("condition") or "")}
            for m in (data.get("match_columns") or [])
        ],
        function_name=str(definition.get("function_name") or ""),
        on_column=str(column_mask.get("on_column") or ""),
        using=[
            {k: v for k, v in (
                ("alias", as_dict(a).get("alias")),
                ("constant", as_dict(a).get("constant")),
            ) if v is not None}
            for a in (definition.get("using") or [])
        ],
        comment=str(data.get("comment") or ""),
        policy_id=str(data.get("id") or ""),
        created_at=millis_to_text(data.get("created_at")),
        created_by=str(data.get("created_by") or ""),
        updated_at=millis_to_text(data.get("updated_at")),
        updated_by=str(data.get("updated_by") or ""),
        # Provenance is positional: a policy listed here but attached elsewhere
        # arrived by inheritance and must not be presented as editable.
        inherited=(attached_name != attach.full_name
                   or attached_type.upper() != attach.securable_type.upper()),
    )


def _principals(values: Sequence[str] | None, what: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        value = str(raw or "").strip()
        if not value:
            continue
        if any(ord(c) < 32 for c in value):
            raise GovernanceError(
                Code.INVALID_INPUT,
                f"{what} chứa ký tự điều khiển không hợp lệ.",
            )
        if len(value) > 255:
            raise GovernanceError(Code.INVALID_INPUT, f"{what} có mục dài quá 255 ký tự.")
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _match_columns(values: Sequence[dict] | None) -> list[dict]:
    out: list[dict] = []
    aliases: set[str] = set()
    for raw in values or ():
        entry = as_dict(raw)
        alias = str(entry.get("alias") or "").strip()
        condition = str(entry.get("condition") or "").strip()
        if not alias or not condition:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Mỗi mục `match_columns` cần cả alias và điều kiện theo thẻ.",
            )
        if any(ord(c) < 32 for c in alias + condition):
            raise GovernanceError(
                Code.INVALID_INPUT, "`match_columns` chứa ký tự điều khiển không hợp lệ.")
        if alias in aliases:
            raise GovernanceError(
                Code.INVALID_INPUT, f"Alias '{alias}' bị khai báo trùng trong `match_columns`.")
        aliases.add(alias)
        out.append({"alias": alias, "condition": condition})
    if len(out) > MAX_MATCH_COLUMNS:
        raise GovernanceError(
            Code.INVALID_INPUT,
            f"`match_columns` tối đa {MAX_MATCH_COLUMNS} mục, đang có {len(out)}. "
            + MATCH_COLUMNS_RULES,
        )
    return out


def _function_arguments(values: Sequence[dict] | None) -> list[dict]:
    out: list[dict] = []
    for raw in values or ():
        entry = as_dict(raw)
        alias = entry.get("alias")
        constant = entry.get("constant")
        has_alias = alias is not None and str(alias).strip() != ""
        has_constant = constant is not None and str(constant) != ""
        if has_alias == has_constant:
            raise GovernanceError(
                Code.INVALID_INPUT,
                "Mỗi tham số của hàm phải là ĐÚNG MỘT trong hai: alias của một cột đã "
                "khai báo, hoặc một hằng số. " + FUNCTION_ARGUMENT_RULES,
            )
        if has_alias:
            out.append({"alias": str(alias).strip()})
        else:
            # Databricks nhận hằng số dưới dạng chuỗi kể cả với tham số kiểu số.
            out.append({"constant": str(constant)})
    return out


def _reason(reason: str) -> str:
    value = (reason or "").strip()
    if not value:
        raise GovernanceError(Code.INVALID_INPUT, "Nhập lý do thay đổi.")
    if len(value) > 500:
        raise GovernanceError(Code.INVALID_INPUT, "Lý do tối đa 500 ký tự.")
    return value


def _draft_state(draft: PolicyDraft, existing: PolicyRow) -> dict:
    """The state the policy would have if this draft were applied."""
    return {
        "name": existing.name,
        "policy_type": existing.policy_type,
        "attach": f"{existing.attach_type}:{existing.attach_fullname}",
        "to_principals": sorted(draft.to_principals),
        "except_principals": sorted(draft.except_principals),
        "when_condition": draft.when_condition,
        "match_columns": draft.match_columns,
        "function_name": draft.function_name,
        "on_column": draft.on_column,
        "using": draft.using,
        "comment": draft.comment,
    }


def _field_texts(field_name: str, existing: PolicyRow, draft: PolicyDraft) -> tuple[str, str]:
    """Before/after text for one updatable field, for the preview table."""
    empty = "(trống)"
    if field_name == "to_principals":
        return (", ".join(existing.to_principals) or empty,
                ", ".join(draft.to_principals) or empty)
    if field_name == "except_principals":
        return (", ".join(existing.except_principals) or empty,
                ", ".join(draft.except_principals) or empty)
    if field_name == "when_condition":
        return (existing.when_condition or "(trống = TRUE)",
                draft.when_condition or "(trống = TRUE)")
    if field_name == "comment":
        return (existing.comment or empty, draft.comment or empty)
    if field_name == "match_columns":
        return (_matchers_text(existing.match_columns) or empty,
                _matchers_text(draft.match_columns) or empty)
    if field_name == "column_mask":
        return (f"{existing.function_name or empty} trên alias {existing.on_column or empty}"
                f" [{_args_text(existing.using)}]",
                f"{draft.function_name or empty} trên alias {draft.on_column or empty}"
                f" [{_args_text(draft.using)}]")
    if field_name == "row_filter":
        return (f"{existing.function_name or empty} [{_args_text(existing.using)}]",
                f"{draft.function_name or empty} [{_args_text(draft.using)}]")
    return (empty, empty)


def _matchers_text(matchers: Sequence[dict]) -> str:
    return " AND ".join(f"{m.get('alias')}: {m.get('condition')}" for m in matchers)


def _args_text(arguments: Sequence[dict]) -> str:
    parts = []
    for a in arguments:
        if a.get("alias"):
            parts.append(f"alias {a['alias']}")
        else:
            parts.append(f"hằng '{a.get('constant')}'")
    return ", ".join(parts) or "không tham số bổ sung"
