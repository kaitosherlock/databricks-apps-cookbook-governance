"""The Unity Catalog privilege catalogue, per securable type.

This replaces the hard-coded six-privilege list the first version of the app
shipped with. Each entry carries a plain-Vietnamese label and a one-line
explanation, so a steward picking a privilege does not have to know the
Databricks reference by heart - but the technical code is always shown next to
the label, because that is what appears in Catalog Explorer and in audit logs.

The per-securable lists follow the Databricks privilege reference. They are the
*offer* list: Unity Catalog remains the authority and will reject anything it
does not accept on a given object.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Risk bands, used to order and to colour-independently flag a privilege.
LOW, MEDIUM, HIGH = "low", "medium", "high"


@dataclass(frozen=True)
class PrivilegeInfo:
    code: str
    label: str
    explanation: str
    risk: str = LOW

    @property
    def display(self) -> str:
        return f"{self.label} — {self.code}"


def _p(code, label, explanation, risk=LOW):
    return PrivilegeInfo(code, label, explanation, risk)


#: Every privilege the app can name, keyed by its Databricks code.
CATALOGUE: dict[str, PrivilegeInfo] = {p.code: p for p in [
    # --- data access -------------------------------------------------------
    _p("SELECT", "Đọc dữ liệu",
       "Đọc được nội dung bảng/view. Đây là quyền xem dữ liệu thật, không chỉ metadata.", HIGH),
    _p("MODIFY", "Ghi và xoá dữ liệu",
       "Thêm, sửa, xoá dòng dữ liệu trong bảng. Không bao gồm quyền đổi cấu trúc đối tượng.", HIGH),
    _p("EXECUTE", "Chạy hàm / dùng mô hình",
       "Gọi được UDF hoặc sử dụng mô hình đã đăng ký.", MEDIUM),
    _p("REFRESH", "Làm mới",
       "Chạy refresh cho materialized view hoặc streaming table.", MEDIUM),
    _p("READ_VOLUME", "Đọc tệp trong volume",
       "Liệt kê và đọc tệp trong volume.", HIGH),
    _p("WRITE_VOLUME", "Ghi tệp vào volume",
       "Tạo, sửa, xoá tệp trong volume.", HIGH),
    _p("READ_FILES", "Đọc tệp ở vị trí lưu trữ",
       "Đọc trực tiếp tệp tại external location hoặc storage credential.", HIGH),
    _p("WRITE_FILES", "Ghi tệp ở vị trí lưu trữ",
       "Ghi trực tiếp tệp tại external location hoặc storage credential.", HIGH),
    _p("READ_PRIVATE_FILES", "Đọc tệp nội bộ",
       "Đọc tệp thuộc vùng lưu trữ nội bộ của Databricks.", HIGH),
    _p("WRITE_PRIVATE_FILES", "Ghi tệp nội bộ",
       "Ghi tệp vào vùng lưu trữ nội bộ của Databricks.", HIGH),
    _p("ACCESS", "Sử dụng credential",
       "Dùng service credential để truy cập dịch vụ bên ngoài.", HIGH),

    # --- traversal ---------------------------------------------------------
    _p("USE_CATALOG", "Vào catalog",
       "Điều kiện cần để đi vào catalog. Bản thân nó KHÔNG cho đọc dữ liệu."),
    _p("USE_SCHEMA", "Vào schema",
       "Điều kiện cần để đi vào schema. Bản thân nó KHÔNG cho đọc dữ liệu."),
    _p("BROWSE", "Xem tên và metadata",
       "Nhìn thấy sự tồn tại, tên, mô tả và tag của đối tượng mà không cần quyền đọc dữ liệu. "
       "Với tài sản dữ liệu, BROWSE chỉ cấp được ở cấp CATALOG."),
    _p("USE_CONNECTION", "Dùng kết nối", "Sử dụng một connection tới nguồn dữ liệu ngoài.", MEDIUM),
    _p("USE_PROVIDER", "Dùng provider", "Xem và dùng các share nhận từ một provider.", MEDIUM),
    _p("USE_RECIPIENT", "Dùng recipient", "Thao tác với một recipient chia sẻ dữ liệu.", MEDIUM),
    _p("USE_SHARE", "Dùng share", "Xem và thao tác với một share.", MEDIUM),
    _p("USE_MARKETPLACE_ASSETS", "Dùng tài sản Marketplace",
       "Truy cập tài sản cài từ Databricks Marketplace.", MEDIUM),
    _p("EXTERNAL_USE_SCHEMA", "Truy cập từ engine ngoài",
       "Cho phép engine bên ngoài đọc bảng qua Iceberg/Delta REST. Mở dữ liệu ra ngoài Databricks.", HIGH),

    # --- administration ----------------------------------------------------
    _p("ALL_PRIVILEGES", "Tất cả quyền dữ liệu",
       "Bao gồm mọi quyền hiện có VÀ quyền Databricks bổ sung sau này trên loại đối tượng này. "
       "KHÔNG bao gồm MANAGE, READ METADATA, EXTERNAL USE SCHEMA, EXTERNAL USE LOCATION "
       "và cũng không phải quyền sở hữu (ownership).", HIGH),
    _p("MANAGE", "Quản trị đối tượng",
       "Xem và thay đổi quyền của người khác trên đối tượng này. Là quyền quản trị, không phải quyền đọc dữ liệu.", HIGH),
    _p("APPLY_TAG", "Gán thẻ (tag)",
       "Gán và gỡ tag trên đối tượng. Tag có thể ảnh hưởng tới chính sách ABAC.", MEDIUM),
    _p("MANAGE_ALLOWLIST", "Quản trị allowlist", "Quản lý allowlist init script/JAR của metastore.", HIGH),
    _p("SET_SHARE_PERMISSION", "Đặt quyền trên share",
       "Thay đổi recipient nào được truy cập một share.", HIGH),

    # --- creation ----------------------------------------------------------
    _p("CREATE_CATALOG", "Tạo catalog", "Tạo catalog mới trong metastore.", MEDIUM),
    _p("CREATE_SCHEMA", "Tạo schema", "Tạo schema mới trong catalog."),
    _p("CREATE_TABLE", "Tạo bảng", "Tạo bảng hoặc view mới."),
    _p("CREATE_VIEW", "Tạo view", "Tạo view mới."),
    _p("CREATE_MATERIALIZED_VIEW", "Tạo materialized view", "Tạo materialized view mới."),
    _p("CREATE_VOLUME", "Tạo volume", "Tạo volume managed mới."),
    _p("CREATE_EXTERNAL_VOLUME", "Tạo volume external", "Tạo volume trỏ tới vị trí lưu trữ ngoài.", MEDIUM),
    _p("CREATE_EXTERNAL_TABLE", "Tạo bảng external", "Tạo bảng trỏ tới vị trí lưu trữ ngoài.", MEDIUM),
    _p("CREATE_FUNCTION", "Tạo hàm", "Tạo UDF mới."),
    _p("CREATE_MODEL", "Tạo mô hình", "Đăng ký mô hình mới."),
    _p("CREATE_CONNECTION", "Tạo kết nối", "Tạo connection tới nguồn dữ liệu ngoài.", HIGH),
    _p("CREATE_FOREIGN_CATALOG", "Tạo foreign catalog", "Tạo catalog federation từ một connection.", MEDIUM),
    _p("CREATE_FOREIGN_SECURABLE", "Tạo securable foreign", "Tạo đối tượng federation tại vị trí này.", MEDIUM),
    _p("CREATE_EXTERNAL_LOCATION", "Tạo external location", "Tạo external location mới.", HIGH),
    _p("CREATE_STORAGE_CREDENTIAL", "Tạo storage credential", "Tạo storage credential mới.", HIGH),
    _p("CREATE_SERVICE_CREDENTIAL", "Tạo service credential", "Tạo service credential mới.", HIGH),
    _p("CREATE_MANAGED_STORAGE", "Tạo vùng lưu trữ managed", "Dùng vị trí này làm managed storage.", HIGH),
    _p("CREATE_SHARE", "Tạo share", "Tạo share Delta Sharing mới.", HIGH),
    _p("CREATE_RECIPIENT", "Tạo recipient", "Tạo recipient nhận dữ liệu chia sẻ.", HIGH),
    _p("CREATE_PROVIDER", "Tạo provider", "Đăng ký provider chia sẻ dữ liệu.", MEDIUM),
    _p("CREATE_CLEAN_ROOM", "Tạo clean room", "Tạo clean room mới.", MEDIUM),

    # --- clean rooms -------------------------------------------------------
    _p("MODIFY_CLEAN_ROOM", "Sửa clean room", "Thay đổi cấu hình và tài sản của clean room.", MEDIUM),
    _p("EXECUTE_CLEAN_ROOM_TASK", "Chạy tác vụ clean room", "Chạy notebook/tác vụ trong clean room.", MEDIUM),

    # --- legacy ------------------------------------------------------------
    _p("USAGE", "Sử dụng (legacy)", "Quyền cũ, tương đương USE_CATALOG/USE_SCHEMA trong mô hình mới."),
    _p("CREATE", "Tạo (legacy)", "Quyền tạo theo mô hình privilege cũ."),
]}


#: Grantable privileges per securable type, following the Databricks privilege
#: reference. Three rules from that reference shape these lists and are easy to
#: get wrong:
#:
#: * BROWSE on *data* objects is grantable at CATALOG level only - never on a
#:   schema, table, volume or function. It is separately grantable on external
#:   locations and clean rooms.
#: * Registered models have no securable type of their own: they are FUNCTIONs,
#:   with a different privilege set (see :data:`MODEL_PRIVILEGES`).
#: * RECIPIENT and PROVIDER accept no privileges at all, so they are absent here.
#:
#: Anything Databricks has not shipped in the pinned SDK's ``Privilege`` enum is
#: dropped by :func:`_prune_to_sdk` below, which is why READ_METADATA and the
#: Beta INSERT/UPDATE/DELETE privileges never reach the picker.
BY_SECURABLE: dict[str, tuple[str, ...]] = {
    "METASTORE": (
        "CREATE_CATALOG", "CREATE_CLEAN_ROOM", "CREATE_CONNECTION", "CREATE_EXTERNAL_LOCATION",
        "CREATE_PROVIDER", "CREATE_RECIPIENT", "CREATE_SERVICE_CREDENTIAL", "CREATE_SHARE",
        "CREATE_STORAGE_CREDENTIAL", "MANAGE_ALLOWLIST", "SET_SHARE_PERMISSION",
        "USE_MARKETPLACE_ASSETS", "USE_PROVIDER", "USE_RECIPIENT", "USE_SHARE",
    ),
    "CATALOG": (
        "ALL_PRIVILEGES", "USE_CATALOG", "USE_SCHEMA", "BROWSE", "SELECT", "MODIFY", "EXECUTE",
        "REFRESH", "READ_VOLUME", "WRITE_VOLUME", "APPLY_TAG", "MANAGE",
        "CREATE_SCHEMA", "CREATE_TABLE", "CREATE_VIEW", "CREATE_MATERIALIZED_VIEW",
        "CREATE_VOLUME", "CREATE_EXTERNAL_VOLUME", "CREATE_FUNCTION", "CREATE_MODEL",
    ),
    "SCHEMA": (
        "ALL_PRIVILEGES", "USE_SCHEMA", "SELECT", "MODIFY", "EXECUTE",
        "REFRESH", "READ_VOLUME", "WRITE_VOLUME", "APPLY_TAG", "MANAGE",
        "CREATE_TABLE", "CREATE_VIEW", "CREATE_MATERIALIZED_VIEW",
        "CREATE_VOLUME", "CREATE_EXTERNAL_VOLUME", "CREATE_FUNCTION", "CREATE_MODEL",
        "EXTERNAL_USE_SCHEMA",
    ),
    "TABLE": ("ALL_PRIVILEGES", "SELECT", "MODIFY", "APPLY_TAG", "MANAGE"),
    "VOLUME": ("ALL_PRIVILEGES", "READ_VOLUME", "WRITE_VOLUME", "APPLY_TAG", "MANAGE"),
    "FUNCTION": ("ALL_PRIVILEGES", "EXECUTE", "APPLY_TAG", "MANAGE"),
    "EXTERNAL_LOCATION": (
        "ALL_PRIVILEGES", "READ_FILES", "WRITE_FILES", "BROWSE", "MANAGE",
        "CREATE_EXTERNAL_TABLE", "CREATE_EXTERNAL_VOLUME", "CREATE_MANAGED_STORAGE",
        "CREATE_FOREIGN_SECURABLE",
    ),
    "STORAGE_CREDENTIAL": (
        "ALL_PRIVILEGES", "READ_FILES", "WRITE_FILES", "MANAGE",
        "CREATE_EXTERNAL_LOCATION", "CREATE_EXTERNAL_TABLE",
    ),
    "CREDENTIAL": ("ALL_PRIVILEGES", "ACCESS", "MANAGE"),
    "CONNECTION": ("ALL_PRIVILEGES", "USE_CONNECTION", "CREATE_FOREIGN_CATALOG", "MANAGE"),
    "CLEAN_ROOM": ("ALL_PRIVILEGES", "MODIFY_CLEAN_ROOM", "EXECUTE_CLEAN_ROOM_TASK", "MANAGE"),
}

#: Registered models are addressed through the FUNCTION securable surface but
#: accept a different privilege set; kept separate so the UI never offers
#: SELECT on a model.
MODEL_PRIVILEGES = ("ALL_PRIVILEGES", "EXECUTE", "APPLY_TAG", "MANAGE")

#: REFRESH is grantable on a materialized view, not on an ordinary table.
_TABLE_TYPE_EXTRA = {
    "MATERIALIZED_VIEW": ("REFRESH",),
}
#: Sub-types that accept no row-level write privilege. Foreign tables are typed
#: TABLE but are read-only through Lakehouse Federation.
_NO_MODIFY = frozenset({"VIEW", "MATERIALIZED_VIEW", "STREAMING_TABLE", "METRIC_VIEW", "FOREIGN"})


def _prune_to_sdk(codes: tuple[str, ...]) -> tuple[str, ...]:
    """Drop anything the pinned SDK's ``Privilege`` enum cannot express.

    The app builds ``PermissionsChange(add=[Privilege(code)])``, which raises on
    an unknown member, so offering a privilege the SDK does not know would fail
    at apply time rather than at selection time. The docs also list privileges
    (READ METADATA, the Beta INSERT/UPDATE/DELETE) that the pinned SDK has not
    shipped - this is what keeps them out of the picker.
    """
    known = _sdk_privilege_names()
    if not known:
        return codes
    return tuple(c for c in codes if c in known)


_SDK_NAMES: frozenset[str] | None = None


def _sdk_privilege_names() -> frozenset[str]:
    global _SDK_NAMES
    if _SDK_NAMES is None:
        try:
            from databricks.sdk.service.catalog import Privilege
            _SDK_NAMES = frozenset(p.value for p in Privilege)
        except Exception:  # pragma: no cover - SDK always present in the app
            _SDK_NAMES = frozenset()
    return _SDK_NAMES


def supported_by_sdk(code: str) -> bool:
    names = _sdk_privilege_names()
    return not names or (code or "").upper() in names


def for_securable(securable_type: str, *, kind: str = "", table_type: str = "") -> tuple[str, ...]:
    """Privileges the UI may offer for one object.

    ``kind`` distinguishes a registered model from a UDF - both are addressed as
    FUNCTION securables but accept different privileges. ``table_type`` narrows
    a TABLE to its sub-type, so the UI never offers MODIFY on a view.
    """
    if kind == "model":
        return _prune_to_sdk(MODEL_PRIVILEGES)
    base = BY_SECURABLE.get((securable_type or "").upper(), ())
    tt = (table_type or "").upper()
    if base and tt:
        if tt in _NO_MODIFY:
            base = tuple(p for p in base if p != "MODIFY")
        base = base + tuple(p for p in _TABLE_TYPE_EXTRA.get(tt, ()) if p not in base)
    return _prune_to_sdk(base)


#: Securable types Unity Catalog accepts no grants on at all.
NOT_GRANTABLE = {
    "RECIPIENT": "Recipient không nhận quyền trực tiếp. Quyền truy cập được điều khiển bằng "
                 "cách cấp SELECT trên SHARE cho recipient đó.",
    "PROVIDER": "Provider không nhận quyền trực tiếp. Quyền được điều khiển ở cấp metastore "
                "bằng CREATE PROVIDER / USE PROVIDER.",
}


def info(code: str) -> PrivilegeInfo:
    code = (code or "").upper()
    return CATALOGUE.get(code, PrivilegeInfo(code, code, "Quyền do Databricks định nghĩa.", MEDIUM))


def display(code: str) -> str:
    return info(code).display


def explain(codes) -> list[dict]:
    rows = []
    for code in codes:
        p = info(code)
        rows.append({"Quyền": p.label, "Mã Databricks": p.code, "Ý nghĩa": p.explanation})
    return rows


#: Privileges whose presence on a parent is a prerequisite for using a child.
TRAVERSAL = ("USE_CATALOG", "USE_SCHEMA")


def traversal_note(kind: str) -> str:
    if kind in ("table", "volume", "function", "model"):
        return (
            "Để thực sự dùng được đối tượng này, principal còn cần USE_CATALOG trên catalog "
            "và USE_SCHEMA trên schema. Ứng dụng không tự cấp thêm hai quyền đó."
        )
    if kind == "schema":
        return "Principal còn cần USE_CATALOG trên catalog cha. Ứng dụng không tự cấp thêm quyền đó."
    return ""


def inheritance_note(kind: str) -> str:
    if kind == "catalog":
        return (
            "Quyền cấp ở catalog được kế thừa xuống mọi schema và đối tượng bên trong, "
            "kể cả đối tượng được tạo trong tương lai."
        )
    if kind == "schema":
        return (
            "Quyền cấp ở schema được kế thừa xuống mọi đối tượng trong schema, "
            "kể cả đối tượng được tạo trong tương lai."
        )
    return "Quyền cấp ở đối tượng này chỉ áp dụng cho chính nó."


#: Privileges that widen access far beyond the object they are granted on.
BROAD = frozenset({"ALL_PRIVILEGES", "MANAGE", "EXTERNAL_USE_SCHEMA"})


def warnings_for(codes, kind: str) -> list[str]:
    """Plain-language warnings shown next to a pending grant."""
    out: list[str] = []
    chosen = {c.upper() for c in codes}
    if "ALL_PRIVILEGES" in chosen:
        out.append(
            "ALL_PRIVILEGES bao gồm cả những quyền Databricks bổ sung trong tương lai, "
            "nhưng KHÔNG bao gồm MANAGE và không phải quyền sở hữu (ownership)."
        )
    if "MANAGE" in chosen:
        out.append(
            "MANAGE cho phép principal tự thay đổi quyền của người khác trên đối tượng này, "
            "kể cả tự cấp SELECT cho chính mình. Hãy xem đây là quyền tương đương quản trị."
        )
        if kind in ("catalog", "schema"):
            out.append(
                "MANAGE cấp ở cấp cha được cấp tường minh xuống mọi đối tượng con."
            )
    if "EXTERNAL_USE_SCHEMA" in chosen:
        out.append(
            "EXTERNAL_USE_SCHEMA mở dữ liệu cho engine bên ngoài Databricks đọc trực tiếp."
        )
    if "APPLY_TAG" in chosen:
        out.append(
            "APPLY_TAG cho phép sửa tag; nếu workspace dùng chính sách ABAC theo tag, "
            "việc này có thể gián tiếp thay đổi phạm vi truy cập."
        )
    if kind in ("catalog", "schema") and chosen & {"SELECT", "MODIFY", "ALL_PRIVILEGES", "READ_VOLUME"}:
        out.append(inheritance_note(kind))
    return out
