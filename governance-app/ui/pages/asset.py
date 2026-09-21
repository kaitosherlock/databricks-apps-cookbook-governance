"""One asset: what it is, what it contains, and how it is protected.

Working information first, raw API payloads behind an expander. A steward
reading this page should be able to answer "what is this and who owns it"
without ever opening the JSON.
"""
from __future__ import annotations

import streamlit as st

from ucg.authz import Action
from ucg.services.assets import AssetService
from ucg.services.base import Context

from .. import nav, picker, state, widgets


def render(ctx: Context):
    assets = AssetService(ctx)

    target = state.current_target()
    if target is None:
        st.markdown("### Thông tin tài sản")
        st.info("Chọn một đối tượng để xem thông tin.", icon=":material/search:")
        target = picker.compact_picker(ctx, assets)
        if target is None:
            return
        st.divider()

    with st.spinner("Đang đọc metadata…"):
        try:
            detail = assets.detail(target)
        except Exception as exc:
            picker.header(ctx, target)
            widgets.show_error(exc, context="Không đọc được metadata của đối tượng này.")
            _change_object(ctx, assets)
            return

    picker.header(ctx, target, detail)
    _actions(ctx, target)
    st.divider()

    tabs = st.tabs(["Tổng quan", "Cấu trúc", "Bảo vệ dữ liệu", "Kỹ thuật"])

    with tabs[0]:
        _overview(detail)
    with tabs[1]:
        _structure(detail)
    with tabs[2]:
        _protection(ctx, detail)
    with tabs[3]:
        widgets.technical_details(detail.raw, label="Metadata JSON",
                                  download=target.full_name)
        st.caption(
            "Dữ liệu thô do API Unity Catalog trả về, dùng để đối chiếu khi cần. "
            "Ứng dụng không đọc nội dung bảng hay tệp để hiển thị trang này."
        )

    st.divider()
    _change_object(ctx, assets)


def _actions(ctx: Context, target):
    cols = st.columns([1.6, 1.6, 1.6, 4])
    with cols[0]:
        nav.link_button("Xem quyền", "permissions", icon=":material/key:",
                        width="stretch", widget_key=f"act_perm_{target.key}")
    with cols[1]:
        allowed = ctx.authz.allows(Action.GRANT, target)
        nav.link_button(
            "Thay đổi quyền", "change_access", icon=":material/edit:",
            width="stretch", widget_key=f"act_change_{target.key}",
            disabled=not allowed,
            help="" if allowed else ctx.authz.check(Action.GRANT, target).reason,
        )
    with cols[2]:
        nav.link_button("Thẻ (tag)", "tags", icon=":material/label:",
                        width="stretch", widget_key=f"act_tags_{target.key}")


def _overview(detail):
    cols = st.columns(2)
    with cols[0]:
        for label, value in detail.summary_fields():
            st.markdown(f"**{label}**  \n{value}")
    with cols[1]:
        if detail.created:
            st.markdown(f"**Tạo lúc**  \n{detail.created}")
        if detail.updated_by:
            st.markdown(f"**Cập nhật bởi**  \n{detail.updated_by}")
        if detail.storage_location:
            st.markdown("**Vị trí lưu trữ**")
            st.caption(detail.storage_location)

    if detail.properties:
        with st.expander("Thuộc tính (properties)"):
            st.dataframe(
                [{"Khoá": k, "Giá trị": v} for k, v in detail.properties.items()],
                hide_index=True, width="stretch",
            )


def _structure(detail):
    if detail.target.kind == "table":
        if not detail.columns:
            widgets.empty_state("empty", what="cột",
                                detail="API không trả về thông tin cột cho đối tượng này.")
            return
        st.caption(f"{len(detail.columns)} cột.")
        widgets.data_table(detail.columns, key=f"cols_{detail.target.key}",
                           download_name=f"{detail.target.full_name}-columns")
    elif detail.target.kind == "function":
        if not detail.columns:
            widgets.empty_state("empty", what="tham số")
            return
        widgets.data_table(detail.columns, key=f"params_{detail.target.key}")
        if detail.raw.get("full_data_type"):
            st.markdown(f"**Kiểu trả về**: `{detail.raw['full_data_type']}`")
    else:
        st.caption("Loại đối tượng này không có cấu trúc cột.")


def _protection(ctx: Context, detail):
    """Legacy row filters / column masks that live on the table itself.

    ABAC policies are a separate mechanism and never appear in these fields, so
    the page says so explicitly instead of implying this is the whole picture.
    """
    st.markdown("**Row filter và column mask gắn trực tiếp trên bảng**")
    if detail.target.kind != "table":
        st.caption("Chỉ áp dụng cho bảng và view.")
        return

    found = False
    if detail.row_filter:
        found = True
        fn = (detail.row_filter or {}).get("function_name", "—")
        st.warning(
            f"Bảng này có **row filter** gắn trực tiếp, dùng hàm `{fn}`.",
            icon=":material/filter_alt:",
        )
        st.caption(
            "Người truy vấn chỉ thấy những dòng hàm này cho phép. "
            "Gỡ filter sẽ để lộ toàn bộ dòng dữ liệu."
        )
    if detail.column_masks:
        found = True
        rows = [
            {"Cột": name, "Hàm mask": (mask or {}).get("function_name", "—")}
            for name, mask in detail.column_masks.items()
        ]
        st.warning(f"{len(rows)} cột đang được che bằng **column mask**.",
                   icon=":material/visibility_off:")
        widgets.data_table(rows, key=f"masks_{detail.target.key}")
        st.caption("Gỡ mask sẽ để lộ giá trị gốc của cột cho người có quyền đọc.")

    if not found:
        st.caption(
            "Không có row filter hoặc column mask nào gắn trực tiếp trên bảng này."
        )

    st.info(
        "Đây **chỉ** là cơ chế gắn trực tiếp trên bảng. Chính sách ABAC tập trung "
        "(row filter / column mask theo tag) là cơ chế khác và không hiện ở đây — "
        "xem mục **Chính sách** để có bức tranh đầy đủ.",
        icon=":material/info:",
    )
    st.caption(
        "Unity Catalog không cho tạo hoặc gỡ filter/mask gắn trực tiếp qua API; "
        "việc đó chỉ làm được bằng câu lệnh SQL ALTER TABLE trong Databricks."
    )

    if detail.constraints:
        st.divider()
        st.markdown("**Ràng buộc (constraints)**")
        widgets.data_table(
            [{"Ràng buộc": str(c)[:200]} for c in detail.constraints],
            key=f"cons_{detail.target.key}",
        )


def _change_object(ctx: Context, assets: AssetService):
    with st.expander("Chọn đối tượng khác", expanded=False):
        picker.compact_picker(ctx, assets)
