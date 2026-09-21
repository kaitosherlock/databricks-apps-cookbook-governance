"""Finding assets and reading one asset's metadata.

Note the shape of what goes over the wire. The Streamlit build rendered
``AssetRef.row()``, whose keys are Vietnamese column headings, because the
consumer was a dataframe. An API has a different consumer, so these endpoints
emit stable machine keys and let the client own presentation. The labels the
backend genuinely computes - ``type_label``, which depends on table_type - are
sent alongside, because that logic belongs to the backend and not to a browser.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ucg.naming import LABELS, Target
from ucg.services.assets import AssetDetail, AssetRef, AssetService
from ucg.services.base import Context

from ..http import CTX, ApiError, fail, listing_payload, require_ok

router = APIRouter(prefix="/assets", tags=["assets"])

#: Kinds a picker may ask for, mirroring ui/picker.KIND_CHOICES.
OBJECT_KINDS = ("table", "volume", "function", "model")
LEVEL_KINDS = ("catalog", "schema") + OBJECT_KINDS


def _ref(item: AssetRef) -> dict:
    return {
        "kind": item.kind,
        "name": item.name,
        "full_name": item.full_name,
        "catalog": item.catalog,
        "schema": item.schema,
        "owner": item.owner,
        "comment": item.comment,
        "sub_type": item.sub_type,
        "type_label": item.type_label,
        "browse_only": item.browse_only,
    }


def _target(kind: str, catalog: str, schema: str, name: str) -> Target:
    """Build a validated Target, or refuse. Never trusts the query string."""
    if kind not in LEVEL_KINDS:
        from ucg.errors import Code, GovernanceError

        raise ApiError(GovernanceError(
            Code.INVALID_INPUT, f"Loại đối tượng không hợp lệ: {kind!r}."
        ))
    try:
        if kind == "catalog":
            return Target("catalog", catalog)
        if kind == "schema":
            return Target("schema", catalog, schema)
        return Target(kind, catalog, schema, name)
    except Exception as exc:
        raise fail(exc) from None


@router.get("/catalogs")
def catalogs(ctx: Context = CTX) -> dict:
    service = AssetService(ctx)
    listing = require_ok(service.catalogs())
    return listing_payload(listing, [_ref(c) for c in listing.items])


@router.get("/schemas")
def schemas(catalog: str = Query(min_length=1), ctx: Context = CTX) -> dict:
    service = AssetService(ctx)
    listing = require_ok(service.schemas(catalog))
    return listing_payload(listing, [_ref(s) for s in listing.items])


@router.get("/objects")
def objects(
    catalog: str = Query(min_length=1),
    schema: str = Query(min_length=1),
    kind: str = Query(default="table"),
    ctx: Context = CTX,
) -> dict:
    if kind not in OBJECT_KINDS:
        from ucg.errors import Code, GovernanceError

        raise ApiError(GovernanceError(
            Code.INVALID_INPUT, f"Loại đối tượng không hợp lệ: {kind!r}."
        ))
    service = AssetService(ctx)
    listing = require_ok(service.objects(catalog, schema, kind))
    return listing_payload(listing, [_ref(o) for o in listing.items])


@router.get("/search")
def search(
    catalog: str = Query(min_length=1),
    schema: str = Query(min_length=1),
    term: str = Query(default=""),
    kinds: list[str] = Query(default=["table"]),
    ctx: Context = CTX,
) -> dict:
    """Name search inside one schema.

    Scoped deliberately: Unity Catalog has no cross-metastore search, so a
    workspace-wide search would be both slow and incomplete. The client says so
    rather than implying it searched everything.
    """
    chosen = tuple(k for k in kinds if k in OBJECT_KINDS)
    if not chosen:
        from ucg.errors import Code, GovernanceError

        raise ApiError(GovernanceError(
            Code.INVALID_INPUT, "Chọn ít nhất một loại đối tượng."
        ))
    service = AssetService(ctx)
    listing = require_ok(service.search(catalog, schema, term, chosen))
    return listing_payload(listing, [_ref(o) for o in listing.items])


def _detail_payload(detail: AssetDetail) -> dict:
    target = detail.target
    return {
        "target": {
            "kind": target.kind,
            "catalog": target.catalog,
            "schema": target.schema,
            "name": target.name,
            "full_name": target.full_name,
            "key": target.key,
            "label": target.label,
        },
        "owner": detail.owner,
        "comment": detail.comment,
        "sub_type": detail.sub_type,
        "type_label": detail.type_label,
        "created": detail.created,
        "updated": detail.updated,
        "updated_by": detail.updated_by,
        "storage_location": detail.storage_location,
        "columns": detail.columns,
        "properties": detail.properties,
        "constraints": [str(c)[:500] for c in detail.constraints],
        "summary_fields": [
            {"label": label, "value": value} for label, value in detail.summary_fields()
        ],
        "protection": {
            # Legacy, table-attached mechanisms only. ABAC policies are a
            # separate system and deliberately absent here; the UI says so.
            "row_filter": detail.row_filter,
            "column_masks": [
                {"column": name, "function": (mask or {}).get("function_name", "")}
                for name, mask in (detail.column_masks or {}).items()
            ],
        },
        "pipeline_managed": detail.is_pipeline_managed,
        "managed_by": detail.managed_by,
        "raw": detail.raw,
    }


@router.get("/detail")
def detail(
    kind: str = Query(default="table"),
    catalog: str = Query(default=""),
    schema: str = Query(default=""),
    name: str = Query(default=""),
    ctx: Context = CTX,
) -> dict:
    target = _target(kind, catalog, schema, name)
    service = AssetService(ctx)
    try:
        return _detail_payload(service.detail(target))
    except Exception as exc:
        raise fail(exc) from None


@router.get("/kinds")
def kinds() -> dict:
    """Pickable kinds and their labels, so the client holds no hardcoded list."""
    return {
        "objects": [{"kind": k, "label": LABELS.get(k, k)} for k in OBJECT_KINDS],
        "levels": [{"kind": k, "label": LABELS.get(k, k)} for k in LEVEL_KINDS],
    }
