"""Unity Catalog object identity: kinds, securable types and validated names.

Two different name spaces meet here and they must not be confused:

* the **UC full name** (``catalog.schema.table``) used by the REST/SDK surface,
  where components are plain strings joined by dots;
* the **SQL identifier** (``` `catalog`.`schema`.`table` ```) used when a module
  has to build a documented SQL statement.

:func:`sql_identifier` is the only place a name is ever turned into SQL, and it
refuses anything it cannot quote safely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import Code, GovernanceError

#: Application-level asset kind -> the ``securable_type`` string the grants and
#: policy APIs expect. Kinds that Unity Catalog does not expose as securables at
#: all are absent from :data:`SECURABLE_TYPE`.
SECURABLE_TYPE = {
    "metastore": "METASTORE",
    "catalog": "CATALOG",
    "schema": "SCHEMA",
    "table": "TABLE",
    "volume": "VOLUME",
    "function": "FUNCTION",
    "model": "FUNCTION",  # probed at runtime; see capabilities.MODEL_GRANTS
    "connection": "CONNECTION",
    "external_location": "EXTERNAL_LOCATION",
    "storage_credential": "STORAGE_CREDENTIAL",
    "credential": "CREDENTIAL",
    "share": "SHARE",
    "recipient": "RECIPIENT",
    "provider": "PROVIDER",
    "clean_room": "CLEAN_ROOM",
    "pipeline": "PIPELINE",
    "external_metadata": "EXTERNAL_METADATA",
}

#: How many dot-separated components a kind's full name has.
DEPTH = {
    "metastore": 0,
    "catalog": 1,
    "schema": 2,
    "table": 3,
    "volume": 3,
    "function": 3,
    "model": 3,
    # Metastore-level securables are addressed by a single bare name.
    "connection": 1,
    "external_location": 1,
    "storage_credential": 1,
    "credential": 1,
    "share": 1,
    "recipient": 1,
    "provider": 1,
    "clean_room": 1,
}

#: Kinds that live inside a catalog and therefore obey the catalog scope filter.
CATALOG_SCOPED = frozenset({"catalog", "schema", "table", "volume", "function", "model"})

LABELS = {
    "metastore": "Metastore",
    "catalog": "Catalog",
    "schema": "Schema",
    "table": "Bảng / View",
    "volume": "Volume",
    "function": "Hàm (function)",
    "model": "Mô hình đã đăng ký",
    "connection": "Kết nối",
    "external_location": "External location",
    "storage_credential": "Storage credential",
    "credential": "Credential",
    "share": "Share",
    "recipient": "Recipient",
    "provider": "Provider",
    "clean_room": "Clean room",
}

#: TableInfo.table_type -> a label a data steward actually recognises.
TABLE_TYPE_LABELS = {
    "MANAGED": "Bảng managed",
    "EXTERNAL": "Bảng external",
    "VIEW": "View",
    "MATERIALIZED_VIEW": "Materialized view",
    "STREAMING_TABLE": "Streaming table",
    "FOREIGN": "Bảng foreign (federation)",
    "METRIC_VIEW": "Metric view",
    "MANAGED_SHALLOW_CLONE": "Shallow clone (managed)",
    "EXTERNAL_SHALLOW_CLONE": "Shallow clone (external)",
}

#: Table types whose contents are produced by something outside this app.
PIPELINE_MANAGED_TABLE_TYPES = frozenset({"STREAMING_TABLE", "MATERIALIZED_VIEW"})

#: Unity Catalog names are permissive, but not unbounded. Reject control
#: characters, dots (they are the separator), and backticks (SQL quoting).
_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f.`]")
MAX_COMPONENT = 255


def validate_component(value: str, what: str = "Tên") -> str:
    value = (value or "").strip()
    if not value:
        raise GovernanceError(Code.INVALID_NAME, f"{what} không được để trống.")
    if len(value) > MAX_COMPONENT:
        raise GovernanceError(Code.INVALID_NAME, f"{what} vượt quá {MAX_COMPONENT} ký tự.")
    if _FORBIDDEN.search(value):
        raise GovernanceError(
            Code.INVALID_NAME,
            f"{what} chứa ký tự không hợp lệ (dấu chấm, dấu backtick hoặc ký tự điều khiển).",
        )
    return value


def sql_identifier(*components: str) -> str:
    """Quote validated components into a SQL identifier.

    Every component has already been through :func:`validate_component`, which
    rejects backticks, so the quoting below cannot be escaped out of. String
    *values* are never built this way - those are bound as parameters.
    """
    parts = [validate_component(c, "Thành phần tên") for c in components if c]
    if not parts:
        raise GovernanceError(Code.INVALID_NAME, "Thiếu tên đối tượng.")
    return ".".join(f"`{p}`" for p in parts)


@dataclass(frozen=True)
class Target:
    """A validated Unity Catalog object reference."""

    kind: str
    catalog: str = ""
    schema: str = ""
    name: str = ""

    def __post_init__(self):
        if self.kind not in DEPTH:
            raise GovernanceError(Code.INVALID_INPUT, f"Loại đối tượng không hợp lệ: {self.kind!r}.")
        depth = DEPTH[self.kind]
        given = [x for x in (self.catalog, self.schema, self.name) if x]
        if len(given) != depth:
            raise GovernanceError(
                Code.INVALID_NAME,
                f"{LABELS.get(self.kind, self.kind)} cần đúng {depth} thành phần tên, nhận được {len(given)}.",
            )
        for value in given:
            validate_component(value)
        # Components must be left-packed: no schema without a catalog.
        if self.name and not self.schema and depth == 3:
            raise GovernanceError(Code.INVALID_NAME, "Thiếu schema.")
        if self.schema and not self.catalog:
            raise GovernanceError(Code.INVALID_NAME, "Thiếu catalog.")

    @property
    def components(self) -> tuple[str, ...]:
        return tuple(x for x in (self.catalog, self.schema, self.name) if x)

    @property
    def full_name(self) -> str:
        return ".".join(self.components)

    @property
    def securable_type(self) -> str:
        try:
            return SECURABLE_TYPE[self.kind]
        except KeyError:  # pragma: no cover - guarded by __post_init__
            raise GovernanceError(Code.CAPABILITY_UNAVAILABLE, "Loại đối tượng này không phải securable.")

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, self.kind)

    @property
    def sql_name(self) -> str:
        return sql_identifier(*self.components)

    @property
    def parent(self) -> "Target | None":
        if self.kind in ("table", "volume", "function", "model"):
            return Target("schema", self.catalog, self.schema)
        if self.kind == "schema":
            return Target("catalog", self.catalog)
        return None

    def ancestors(self) -> list["Target"]:
        chain, node = [], self.parent
        while node is not None:
            chain.append(node)
            node = node.parent
        return chain

    @property
    def key(self) -> str:
        """Stable identifier for session state and plan binding."""
        return f"{self.kind}:{self.full_name}"

    @classmethod
    def parse(cls, kind: str, full_name: str) -> "Target":
        parts = [p for p in (full_name or "").split(".")]
        expected = DEPTH.get(kind)
        if expected is None:
            raise GovernanceError(Code.INVALID_INPUT, f"Loại đối tượng không hợp lệ: {kind!r}.")
        if len(parts) != expected:
            raise GovernanceError(
                Code.INVALID_NAME,
                f"{LABELS.get(kind, kind)} cần {expected} thành phần, '{full_name}' có {len(parts)}.",
            )
        padded = parts + [""] * (3 - len(parts))
        if expected == 1:
            # Metastore-level securables carry their single name in `catalog`
            # so the dataclass invariant (left-packed) holds.
            return cls(kind, parts[0])
        return cls(kind, padded[0], padded[1], padded[2])


def table_kind_label(table_type: str | None) -> str:
    return TABLE_TYPE_LABELS.get(str(table_type or "").upper(), "Bảng")
