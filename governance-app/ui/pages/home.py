"""Home: find a data asset and see who can reach it.

This is the app's front door, so it answers the two questions a steward
actually arrives with - "where is this table?" and "who has access to it?" -
without a detour through a dashboard of decorative numbers.
"""
from __future__ import annotations

import streamlit as st

from ucg.naming import Target
from ucg.services.assets import AssetService
from ucg.services.base import Context

from .. import chrome, nav, picker, state, widgets


def render(ctx: Context):
    chrome.render_banner(ctx)
    st.divider()

    st.markdown("### Tìm tài sản dữ liệu")
    st.caption(
        "Chọn catalog và schema, rồi tìm theo tên. Ứng dụng chỉ tải dữ liệu của "
        "phạm vi bạn đang chọn, không quét toàn bộ workspace."
    )

    assets = AssetService(ctx)
    _notice()

    cols = st.columns([2, 2])
    with cols[0]:
        catalog = picker.catalog_selector(ctx, assets)
    if not catalog:
        return
    with cols[1]:
        schema = picker.schema_selector(ctx, assets, catalog)
    if not schema:
        return

    filters = state.filters()
    search_cols = st.columns([3, 3, 1])
    with search_cols[0]:
        term = st.text_input(
            "Tìm theo tên", value=filters.get("search", ""),
            placeholder="Ví dụ: routes", key="home_search",
            help="Tìm trong tên và mô tả của các đối tượng thuộc schema đã chọn.",
        )
        state.set_filter("search", term)
    with search_cols[1]:
        kinds = st.multiselect(
            "Loại đối tượng", [k for k, _ in picker.KIND_CHOICES],
            default=filters.get("kinds") or ["table"],
            format_func=lambda k: picker.KIND_LABELS[k], key="home_kinds",
        )
        state.set_filter("kinds", kinds)
    with search_cols[2]:
        st.write("")
        if st.button("Làm mới", icon=":material/refresh:", key="home_refresh",
                     width="stretch"):
            state.clear_caches()
            st.rerun()

    if not kinds:
        st.info("Chọn ít nhất một loại đối tượng để xem kết quả.", icon=":material/filter_alt:")
        return

    with st.spinner("Đang đọc danh sách tài sản…"):
        listing = assets.search(catalog, schema, term, tuple(kinds))

    if not listing.ok:
        widgets.show_error(listing.error, context="Không tải được danh sách tài sản.")
        return
    if listing.empty:
        if term:
            st.info(
                f"Không có đối tượng nào khớp “{term}” trong `{catalog}.{schema}`.",
                icon=":material/search_off:",
            )
            st.caption("Thử bỏ bớt từ khoá, hoặc chọn loại đối tượng khác.")
        else:
            widgets.empty_state("empty", what="đối tượng")
        return

    st.caption(f"{len(listing.items)} đối tượng trong `{catalog}.{schema}`.")
    _results(ctx, listing)
    widgets.completeness_note(listing)


def _results(ctx: Context, listing):
    for ref in listing.items[:100]:
        with st.container(border=True):
            cols = st.columns([4, 2, 2, 1.6])
            with cols[0]:
                st.markdown(f"**{ref.name}**")
                # Full path always shown: two schemas can hold the same name.
                st.caption(ref.full_name)
            cols[1].markdown(f"{ref.type_label}")
            cols[2].markdown(f"Chủ sở hữu  \n{ref.owner or '—'}")
            with cols[3]:
                if st.button("Mở", key=f"open_{ref.kind}_{ref.full_name}",
                             width="stretch", icon=":material/arrow_forward:"):
                    state.select_target(ref.target)
                    nav.goto("asset")
            if ref.comment:
                st.caption(ref.comment[:200])

    if len(listing.items) > 100:
        st.caption(
            f"Đang hiển thị 100/{len(listing.items)} kết quả. Dùng ô tìm kiếm để thu hẹp."
        )


def _notice():
    payload = state.pop_notice()
    if not payload:
        return
    st.success(payload.get("text", "Hoàn tất."), icon=":material/check_circle:")
