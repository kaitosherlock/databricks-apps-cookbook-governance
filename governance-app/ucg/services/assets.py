"""Browsing and describing Unity Catalog data assets.

Covers catalogs, schemas, tables/views/materialized views/streaming tables,
volumes, functions and registered models. Two deliberate restraints:

* metadata only - the app never reads table rows or volume files to describe
  an object;
* no uniform CRUD - a table has no update endpoint, so "rename" and "change
  owner" are simply not offered for tables, rather than offered and failing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import Code, GovernanceError
from ..naming import (
    PIPELINE_MANAGED_TABLE_TYPES, Target, table_kind_label, validate_component,
)
from ..paging import take
from .base import Listing, Service, as_dict, enum_value, millis_to_text, now_text

#: How many objects one screen pulls before saying "refine your search".
PAGE_LIMIT = 300


@dataclass
class AssetRef:
    """A listable asset, with everything a picker needs and nothing more."""

    kind: str
    name: str
    full_name: str
    catalog: str = ""
    schema: str = ""
    owner: str = ""
    comment: str = ""
    sub_type: str = ""
    browse_only: bool = False

    @property
    def target(self) -> Target:
        return Target(self.kind, self.catalog, self.schema, self.name)

    @property
    def type_label(self) -> str:
        if self.kind == "table":
            return table_kind_label(self.sub_type)
        from ..naming import LABELS
        return LABELS.get(self.kind, self.kind)

    def row(self) -> dict:
        return {
            "Tên": self.name,
            "Loại": self.type_label,
            "Đường dẫn đầy đủ": self.full_name,
            "Chủ sở hữu": self.owner or "—",
            "Mô tả": (self.comment or "")[:160],
        }


@dataclass
class AssetDetail:
    """Everything one asset page needs."""

    target: Target
    raw: dict
    owner: str = ""
    comment: str = ""
    sub_type: str = ""
    created: str = ""
    updated: str = ""
    updated_by: str = ""
    columns: list = field(default_factory=list)
    properties: dict = field(default_factory=dict)
    #: Legacy, table-attached row filter (NOT an ABAC policy).
    row_filter: dict | None = None
    #: Column name -> legacy column mask.
    column_masks: dict = field(default_factory=dict)
    constraints: list = field(default_factory=list)
    managed_by: str = ""
    storage_location: str = ""
    view_definition_available: bool = False

    @property
    def type_label(self) -> str:
        if self.target.kind == "table":
            return table_kind_label(self.sub_type)
        return self.target.label

    @property
    def is_pipeline_managed(self) -> bool:
        return bool(self.managed_by)

    def summary_fields(self) -> list[tuple[str, str]]:
        out = [
            ("Loại", self.type_label),
            ("Chủ sở hữu", self.owner or "Chưa xác định"),
        ]
        if self.updated:
            out.append(("Cập nhật lần cuối", self.updated))
        if self.managed_by:
            out.append(("Được quản lý bởi", self.managed_by))
        return out


class AssetService(Service):
    capability_keys = ("assets.browse", "assets.metadata")

    # -- listing ----------------------------------------------------------
    def catalogs(self) -> Listing:
        def run():
            items, more = take(
                self.w.catalogs.list(include_browse=True, max_results=0), PAGE_LIMIT
            )
            refs = []
            for c in items:
                data = as_dict(c)
                name = data.get("name") or ""
                if not self.settings.in_scope(name):
                    continue
                refs.append(AssetRef(
                    kind="catalog", name=name, full_name=name, catalog=name,
                    owner=data.get("owner") or "", comment=data.get("comment") or "",
                    sub_type=enum_value(data.get("catalog_type")),
                    browse_only=bool(data.get("browse_only")),
                ))
            refs.sort(key=lambda r: r.name.lower())
            return Listing(refs, "truncated" if more else "complete", now_text())

        return self.listing(run)

    def schemas(self, catalog: str) -> Listing:
        self.authz.require_scope(catalog)
        validate_component(catalog, "Tên catalog")

        def run():
            items, more = take(
                self.w.schemas.list(catalog_name=catalog, include_browse=True, max_results=0),
                PAGE_LIMIT,
            )
            refs = []
            for s in items:
                data = as_dict(s)
                name = data.get("name") or ""
                refs.append(AssetRef(
                    kind="schema", name=name, full_name=f"{catalog}.{name}",
                    catalog=catalog, schema=name,
                    owner=data.get("owner") or "", comment=data.get("comment") or "",
                    browse_only=bool(data.get("browse_only")),
                ))
            refs.sort(key=lambda r: r.name.lower())
            return Listing(refs, "truncated" if more else "complete", now_text())

        return self.listing(run)

    def objects(self, catalog: str, schema: str, kind: str) -> Listing:
        """Tables/views, volumes, functions or registered models in one schema."""
        self.authz.require_scope(catalog)
        validate_component(catalog, "Tên catalog")
        validate_component(schema, "Tên schema")
        if kind not in ("table", "volume", "function", "model"):
            raise GovernanceError(Code.INVALID_INPUT, f"Loại đối tượng không hỗ trợ: {kind}.")

        def run():
            iterator = self._object_iterator(catalog, schema, kind)
            items, more = take(iterator, PAGE_LIMIT)
            refs = []
            for o in items:
                data = as_dict(o)
                name = data.get("name") or ""
                refs.append(AssetRef(
                    kind=kind, name=name, full_name=f"{catalog}.{schema}.{name}",
                    catalog=catalog, schema=schema,
                    owner=data.get("owner") or "", comment=data.get("comment") or "",
                    sub_type=enum_value(data.get("table_type") or data.get("volume_type")),
                    browse_only=bool(data.get("browse_only")),
                ))
            refs.sort(key=lambda r: r.name.lower())
            return Listing(refs, "truncated" if more else "complete", now_text())

        return self.listing(run)

    def _object_iterator(self, catalog: str, schema: str, kind: str):
        if kind == "table":
            # omit_columns keeps the list call cheap; the detail call fetches them.
            return self.w.tables.list(
                catalog_name=catalog, schema_name=schema, max_results=0,
                include_browse=True, omit_columns=True, omit_properties=True,
            )
        if kind == "volume":
            return self.w.volumes.list(
                catalog_name=catalog, schema_name=schema, max_results=0, include_browse=True
            )
        if kind == "function":
            return self.w.functions.list(
                catalog_name=catalog, schema_name=schema, max_results=0, include_browse=True
            )
        return self.w.registered_models.list(
            catalog_name=catalog, schema_name=schema, max_results=0, include_browse=True
        )

    def search(self, catalog: str, schema: str, term: str, kinds: tuple[str, ...]) -> Listing:
        """Name search within the chosen catalog/schema.

        Scoped on purpose: Unity Catalog has no cross-metastore search API, and
        walking every schema on each keystroke would hammer the workspace.
        """
        term = (term or "").strip().lower()
        collected: list[AssetRef] = []
        completeness = "complete"
        for kind in kinds:
            if kind in ("catalog", "schema"):
                continue
            result = self.objects(catalog, schema, kind)
            if not result.ok:
                completeness = "partial_permission"
                continue
            if result.completeness != "complete":
                completeness = result.completeness
            collected.extend(
                r for r in result.items
                if not term or term in r.name.lower() or term in (r.comment or "").lower()
            )
        collected.sort(key=lambda r: (r.kind, r.name.lower()))
        return Listing(collected, completeness, now_text())

    # -- detail -----------------------------------------------------------
    def detail(self, target: Target) -> AssetDetail:
        self.authz.require_scope(target.catalog)
        raw = as_dict(self.read(lambda: self._fetch(target)))
        detail = AssetDetail(
            target=target,
            raw=raw,
            owner=raw.get("owner") or "",
            comment=raw.get("comment") or "",
            sub_type=enum_value(raw.get("table_type") or raw.get("volume_type")
                                or raw.get("catalog_type")),
            created=millis_to_text(raw.get("created_at")),
            updated=millis_to_text(raw.get("updated_at")),
            updated_by=raw.get("updated_by") or "",
            properties=raw.get("properties") or {},
            storage_location=raw.get("storage_location") or "",
        )

        if target.kind == "table":
            detail.columns = self._columns(raw)
            detail.row_filter = raw.get("row_filter") or None
            detail.column_masks = {
                c.get("name"): c.get("mask")
                for c in (raw.get("columns") or []) if c.get("mask")
            }
            detail.constraints = raw.get("table_constraints") or []
            detail.view_definition_available = bool(raw.get("view_definition"))
            if detail.sub_type in PIPELINE_MANAGED_TABLE_TYPES or raw.get("pipeline_id"):
                detail.managed_by = (
                    f"Lakeflow pipeline {raw.get('pipeline_id')}"
                    if raw.get("pipeline_id") else "Lakeflow pipeline"
                )
        elif target.kind == "function":
            detail.columns = self._function_params(raw)
        return detail

    def _fetch(self, target: Target):
        kind = target.kind
        if kind == "catalog":
            return self.w.catalogs.get(name=target.full_name, include_browse=True)
        if kind == "schema":
            return self.w.schemas.get(full_name=target.full_name, include_browse=True)
        if kind == "table":
            return self.w.tables.get(full_name=target.full_name, include_browse=True)
        if kind == "volume":
            return self.w.volumes.read(name=target.full_name, include_browse=True)
        if kind == "function":
            return self.w.functions.get(name=target.full_name, include_browse=True)
        if kind == "model":
            return self.w.registered_models.get(
                full_name=target.full_name, include_browse=True, include_aliases=True
            )
        raise GovernanceError(Code.INVALID_INPUT, f"Loại đối tượng không hỗ trợ: {kind}.")

    @staticmethod
    def _columns(raw: dict) -> list[dict]:
        rows = []
        for c in raw.get("columns") or []:
            rows.append({
                "Cột": c.get("name"),
                "Kiểu": c.get("type_text"),
                "Cho phép NULL": "Có" if c.get("nullable") else "Không",
                "Mô tả": c.get("comment") or "",
                "Column mask": (c.get("mask") or {}).get("function_name", "") if c.get("mask") else "",
            })
        return rows

    @staticmethod
    def _function_params(raw: dict) -> list[dict]:
        params = ((raw.get("input_params") or {}).get("parameters")) or []
        return [{
            "Tham số": p.get("name"),
            "Kiểu": p.get("type_text"),
            "Mô tả": p.get("comment") or "",
        } for p in params]

    # -- table type facts the rest of the app branches on -----------------
    def table_type(self, target: Target) -> str:
        if target.kind != "table":
            return ""
        return enum_value(as_dict(self.read(lambda: self.w.tables.get(full_name=target.full_name))).get("table_type"))

    def owner_of(self, target: Target) -> str:
        return self.detail(target).owner

    # -- editing ----------------------------------------------------------
    #: Kinds whose owner/comment can be changed through a REST update. TABLE is
    #: absent on purpose: the Tables API has no update operation at all.
    EDITABLE = ("catalog", "schema", "volume", "model")
    OWNER_EDITABLE = ("catalog", "schema", "volume", "function", "model")

    def can_edit_comment(self, target: Target) -> tuple[bool, str]:
        if target.kind not in self.EDITABLE:
            return False, (
                "API Unity Catalog không hỗ trợ sửa mô tả cho loại đối tượng này. "
                "Với bảng và view, dùng COMMENT ON trong Databricks SQL."
            )
        return True, ""

    def can_transfer_owner(self, target: Target) -> tuple[bool, str]:
        if target.kind in self.OWNER_EDITABLE:
            return True, ""
        if target.kind == "table":
            return False, (
                "API Tables không có thao tác cập nhật. Chuyển quyền sở hữu bảng/view "
                "chỉ thực hiện được bằng câu lệnh SQL ALTER TABLE … OWNER TO."
            )
        return False, "Loại đối tượng này không hỗ trợ chuyển quyền sở hữu qua API."

    def update_comment(self, target: Target, comment: str):
        ok, reason = self.can_edit_comment(target)
        if not ok:
            raise GovernanceError(Code.CAPABILITY_UNAVAILABLE, reason)
        if len(comment or "") > 4000:
            raise GovernanceError(Code.INVALID_INPUT, "Mô tả tối đa 4000 ký tự.")
        kind = target.kind
        if kind == "catalog":
            self.w.catalogs.update(name=target.full_name, comment=comment)
        elif kind == "schema":
            self.w.schemas.update(full_name=target.full_name, comment=comment)
        elif kind == "volume":
            self.w.volumes.update(name=target.full_name, comment=comment)
        else:
            self.w.registered_models.update(full_name=target.full_name, comment=comment)

    def update_owner(self, target: Target, owner: str):
        ok, reason = self.can_transfer_owner(target)
        if not ok:
            raise GovernanceError(Code.CAPABILITY_UNAVAILABLE, reason)
        owner = (owner or "").strip()
        if not owner:
            raise GovernanceError(Code.INVALID_INPUT, "Nhập principal sẽ nhận quyền sở hữu.")
        kind = target.kind
        if kind == "catalog":
            self.w.catalogs.update(name=target.full_name, owner=owner)
        elif kind == "schema":
            self.w.schemas.update(full_name=target.full_name, owner=owner)
        elif kind == "volume":
            self.w.volumes.update(name=target.full_name, owner=owner)
        elif kind == "function":
            self.w.functions.update(name=target.full_name, owner=owner)
        else:
            self.w.registered_models.update(full_name=target.full_name, owner=owner)
