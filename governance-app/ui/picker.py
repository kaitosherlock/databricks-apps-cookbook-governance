"""Choosing an object once, and keeping that choice.

Loading is lazy by design: catalogs on entry, schemas only for the chosen
catalog, objects only for the chosen schema. Unity Catalog has no cross-
metastore search API, so walking every schema on each keystroke would hammer
the workspace and still be incomplete.
"""
from __future__ import annotations

import streamlit as st

from ucg.naming import LABELS, Target
from ucg.services.assets import AssetService
from ucg.services.base import Context

from . import state, widgets

KIND_CHOICES = [
    ("table", "Bảng / View"),
    ("volume", "Volume"),
    ("function", "Hàm"),
    ("model", "Mô hình"),
]
KIND_LABELS = dict(KIND_CHOICES)


def _cached(ctx: Context, key: str, produce):
    hit = state.cache_get(key)
    if hit is not None:
        return hit
    return state.cache_put(key, produce())


def catalog_selector(ctx: Context, assets: AssetService) -> str | None:
    """Pick a catalog. Returns None when there is nothing selectable."""
    listing = _cached(ctx, "catalogs", assets.catalogs)
    if not listing.ok:
        widgets.show_error(listing.error, context="Không đọc được danh sách catalog.")
        return None
    if listing.empty:
        _no_catalogs(ctx)
        return None

    names = [c.name for c in listing.items]
    sel = state.selection()
    index = names.index(sel["catalog"]) if sel.get("catalog") in names else 0
    chosen = st.selectbox(
        "Catalog", names, index=index, key="pick_catalog",
        help="Chỉ hiển thị catalog mà danh tính thực thi nhìn thấy được.",
    )
    if chosen != sel.get("catalog"):
        state.set_selection(catalog=chosen, schema="", name="")
    return chosen


def schema_selector(ctx: Context, assets: AssetService, catalog: str) -> str | None:
    listing = _cached(ctx, f"schemas:{catalog}", lambda: assets.schemas(catalog))
    if not listing.ok:
        widgets.show_error(listing.error, context="Không đọc được danh sách schema.")
        return None
    if listing.empty:
        widgets.empty_state("empty", what="schema")
        return None

    names = [s.name for s in listing.items]
    sel = state.selection()
    index = names.index(sel["schema"]) if sel.get("schema") in names else 0
    chosen = st.selectbox("Schema", names, index=index, key=f"pick_schema_{catalog}")
    if chosen != sel.get("schema"):
        state.set_selection(schema=chosen, name="")
    return chosen


def object_selector(ctx: Context, assets: AssetService, catalog: str, schema: str,
                    kind: str) -> str | None:
    listing = _cached(
        ctx, f"objects:{catalog}.{schema}:{kind}",
        lambda: assets.objects(catalog, schema, kind),
    )
    if not listing.ok:
        widgets.show_error(listing.error, context=f"Không đọc được danh sách {KIND_LABELS.get(kind, kind)}.")
        return None
    if listing.empty:
        st.selectbox(
            KIND_LABELS.get(kind, kind),
            [f"— Chưa có {KIND_LABELS.get(kind, kind).lower()} nào —"],
            index=0,
            disabled=True,
            key=f"pick_object_empty_{catalog}_{schema}_{kind}",
            help=f"Schema {catalog}.{schema} chưa có {KIND_LABELS.get(kind, kind).lower()} nào.",
        )
        return None

    names = [o.name for o in listing.items]
    sel = state.selection()
    index = names.index(sel["name"]) if sel.get("name") in names else 0
    chosen = st.selectbox(KIND_LABELS.get(kind, kind), names, index=index,
                          key=f"pick_object_{catalog}_{schema}_{kind}")
    if chosen != sel.get("name"):
        state.set_selection(name=chosen)
    return chosen


def compact_picker(ctx: Context, assets: AssetService) -> Target | None:
    """The full chooser, laid out in one row. Used at the top of task pages."""
    cols = st.columns([2.2, 2.2, 2.8, 3.8])
    with cols[0]:
        catalog = catalog_selector(ctx, assets)
    if not catalog:
        return None

    with cols[1]:
        schema = schema_selector(ctx, assets, catalog)
    if not schema:
        return None

    sel = state.selection()
    with cols[2]:
        kinds = [k for k, _ in KIND_CHOICES]
        kind = st.selectbox(
            "Loại", kinds,
            index=kinds.index(sel["kind"]) if sel.get("kind") in kinds else 0,
            format_func=lambda k: KIND_LABELS[k], key="pick_kind",
        )
        if kind != sel.get("kind"):
            state.set_selection(kind=kind, name="")

    with cols[3]:
        name = object_selector(ctx, assets, catalog, schema, state.selection()["kind"])
    if not name:
        st.caption(
            f"Chưa có {KIND_LABELS.get(state.selection()['kind'], 'đối tượng').lower()} nào trong `{catalog}.{schema}`."
        )
        return None

    try:
        return Target(state.selection()["kind"], catalog, schema, name)
    except Exception as exc:
        widgets.show_error(exc)
        return None


def level_picker(ctx: Context, assets: AssetService) -> Target | None:
    """Picker that also allows choosing the catalog or the schema itself.

    Permissions are managed at every level, so the permissions screens need to
    address a catalog or a schema as the target, not only a leaf object.
    """
    levels = [
        ("catalog", "Catalog"),
        ("schema", "Schema"),
        ("table", "Bảng / View"),
        ("volume", "Volume"),
        ("function", "Hàm"),
        ("model", "Mô hình"),
    ]
    codes = [c for c, _ in levels]
    label_map = dict(levels)

    sel = state.selection()
    level = st.radio(
        "Quản lý quyền ở cấp", codes,
        index=codes.index(sel["kind"]) if sel.get("kind") in codes else 2,
        format_func=lambda c: label_map[c], horizontal=True, key="pick_level",
    )
    if level != sel.get("kind"):
        state.set_selection(kind=level)

    cols = st.columns([1.2, 1.2, 1.6])
    with cols[0]:
        catalog = catalog_selector(ctx, assets)
    if not catalog:
        return None
    if level == "catalog":
        return Target("catalog", catalog)

    with cols[1]:
        schema = schema_selector(ctx, assets, catalog)
    if not schema:
        return None
    if level == "schema":
        return Target("schema", catalog, schema)

    with cols[2]:
        name = object_selector(ctx, assets, catalog, schema, level)
    if not name:
        st.caption(
            f"Chưa có {label_map.get(level, level).lower()} nào trong `{catalog}.{schema}`."
        )
        return None
    return Target(level, catalog, schema, name)


def header(ctx: Context, target: Target, detail=None):
    """Object identity at the top of every object page."""
    parts = [("Catalog", target.catalog)]
    if target.schema:
        parts.append(("Schema", target.schema))
    if target.name:
        parts.append((LABELS.get(target.kind, target.kind), target.name))
    widgets.breadcrumb(parts)

    st.markdown(f"## {target.name or target.schema or target.catalog}")

    cols = st.columns([1.8, 3.2, 3])
    type_label = detail.type_label if detail is not None else target.label
    owner = (detail.owner if detail is not None else "") or "Chưa xác định"
    cols[0].markdown(f"**Loại**  \n{type_label}")
    cols[1].markdown(f"**Chủ sở hữu**  \n<span class='no-break-email'>{owner}</span>", unsafe_allow_html=True)
    with cols[2]:
        st.text_input("Tên đầy đủ", value=target.full_name,
                      key=f"fullname_{target.key}",
                      help="Chọn ô và nhấn Ctrl+C để sao chép.")

    if detail is not None:
        st.markdown(detail.comment or "_Chưa có mô tả._")
        if detail.is_pipeline_managed:
            st.info(
                f"Đối tượng này được quản lý bởi {detail.managed_by}. "
                "Thay đổi cấu trúc và dữ liệu phải thực hiện ở pipeline, không phải ở đây.",
                icon=":material/conversion_path:",
            )


def _no_catalogs(ctx: Context):
    st.warning("Chưa nhìn thấy catalog nào.", icon=":material/folder_off:")
    if ctx.settings.catalogs:
        st.markdown(
            "Cấu hình `GOVERNANCE_CATALOGS` đang giới hạn phạm vi ở: "
            f"**{ctx.settings.scope_label}**. Kiểm tra tên catalog có đúng không, "
            "và danh tính thực thi có quyền nhìn thấy chúng không."
        )
    else:
        st.markdown(
            """
Tài khoản dịch vụ của ứng dụng chưa được cấp quyền nhìn thấy catalog nào.

**Việc cần làm trong Catalog Explorer** (do người có thẩm quyền thực hiện):

1. Mở catalog cần quản lý → tab **Permissions**.
2. Cấp `USE CATALOG` và `BROWSE` cho **application ID** của tài khoản dịch vụ ứng dụng.
3. Để đọc và quản lý được quyền, cấp thêm `MANAGE` đúng phạm vi cần quản trị,
   hoặc chuyển quyền sở hữu phù hợp.
            """
        )
        st.caption(
            "Lưu ý: cấp MANAGE ở cấp catalog có phạm vi rất rộng xuống mọi đối tượng con. "
            "Chỉ làm khi đó đúng là chủ đích."
        )
