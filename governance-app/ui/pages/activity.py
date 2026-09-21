"""What this app did, in this session.

Named and framed carefully: Databricks Apps does not persist logs past the
compute lifetime, so this is an operational record of the current session, not
an audit trail. The Databricks audit log is a separate page reading
``system.access.audit``.
"""
from __future__ import annotations

import streamlit as st

from ucg.audit import OperationLog, Status
from ucg.services.base import Context

from .. import nav, widgets


def render(ctx: Context):
    st.markdown("### Thao tác gần đây")
    st.caption("Các thay đổi bạn thực hiện qua ứng dụng trong phiên làm việc này.")

    st.info(OperationLog.DISCLAIMER, icon=":material/info:")

    events = ctx.log.events
    if not events:
        st.info("Chưa có thao tác nào trong phiên này.", icon=":material/history:")
        st.caption(
            "Thao tác chỉ đọc không được ghi ở đây. "
            "Mọi lệnh gọi API đều được Databricks ghi nhận riêng dưới danh tính thực thi."
        )
        return

    unresolved = ctx.log.needing_reconcile()
    if unresolved:
        st.warning(
            f"{len(unresolved)} thao tác chưa xác định được kết quả. "
            "Hãy đọc lại trạng thái thực tế trên Databricks trước khi thực hiện lại — "
            "ứng dụng cố ý không tự động thử lại để tránh thực hiện hai lần.",
            icon=":material/help:",
        )

    widgets.data_table(ctx.log.rows(), key="oplog", download_name="thao-tac-phien-nay")

    st.divider()
    st.markdown("**Chi tiết từng thao tác**")
    for event in events[:50]:
        icon = _icon(event.status)
        with st.expander(
            f"{icon} {event.summary or event.action} · {event.target} · {event.status_label}",
            expanded=False,
        ):
            cols = st.columns(2)
            with cols[0]:
                st.markdown(f"**Người thao tác**  \n{event.actor or '—'}")
                st.markdown(f"**Đối tượng**  \n`{event.target}`")
                if event.principal:
                    st.markdown(f"**Principal nhận quyền**  \n{event.principal}")
            with cols[1]:
                st.markdown(f"**Danh tính thực thi**  \n{event.execution_identity}")
                st.markdown(f"**Thời điểm (UTC)**  \n{event.time_utc[:19].replace('T', ' ')}")
                st.markdown(f"**Kết quả**  \n{event.status_label}")
            if event.reason:
                st.markdown(f"**Lý do**  \n{event.reason}")
            if event.details:
                st.caption(f"Chi tiết: {event.details}")
            st.caption(
                f"Event ID: `{event.event_id}`"
                + (f" · Mã thao tác: `{event.operation_id[:12]}`" if event.operation_id else "")
                + (f" · Mã lỗi: `{event.error_code}`" if event.error_code else "")
            )
            if event.status in Status.NEEDS_RECONCILE:
                st.warning(
                    "Kết quả chưa xác định. Mở **Quyền truy cập** của đối tượng này, "
                    "làm mới, và đối chiếu trạng thái thật trước khi thử lại.",
                    icon=":material/warning:",
                )

    st.divider()
    st.caption(
        "Cần bản ghi kiểm toán đầy đủ và lâu dài? Xem mục **Nhật ký kiểm toán**, "
        "đọc trực tiếp từ `system.access.audit` của Databricks."
    )
    nav.link_button("Mở nhật ký kiểm toán Databricks", "audit",
                    icon=":material/policy:", widget_key="act_to_audit")


def _icon(status: str) -> str:
    return {
        Status.VERIFIED: ":material/verified:",
        Status.APPLIED: ":material/check:",
        Status.FAILED: ":material/error:",
        Status.UNKNOWN: ":material/help:",
        Status.VERIFY_FAILED: ":material/help:",
        Status.APPLYING: ":material/hourglass:",
    }.get(status, ":material/circle:")
