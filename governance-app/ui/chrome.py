"""The persistent frame: who you are, where you are, and what you may do.

The brief is emphatic that a user must never confuse the person using the app
with the identity the API calls run as, and must never mistake PROD for DEV.
Both facts live here, stated in words, and repeated in the sidebar on every
page rather than shown once on entry.
"""
from __future__ import annotations

import streamlit as st

from ucg.config import Role
from ucg.identity import ExecutionIdentity
from ucg.services.base import Context

from . import state
from .session import running_identity

#: Environments that get an unmistakable marker. Never inferred from the host.
_HIGH_RISK = {"PROD", "PRODUCTION"}


def environment_marker(ctx: Context) -> tuple[str, str]:
    """(label, icon) for the configured environment, or an honest blank."""
    env = ctx.settings.environment
    if not env:
        return "Môi trường: chưa khai báo", ":material/help:"
    if env in _HIGH_RISK:
        return f"MÔI TRƯỜNG {env}", ":material/gpp_maybe:"
    return f"Môi trường {env}", ":material/label:"


def mode_marker(ctx: Context) -> tuple[str, str, str]:
    """(label, icon, explanation) for the current write mode."""
    if not ctx.settings.writes_possible:
        return (
            "Chỉ đọc",
            ":material/visibility:",
            ctx.settings.write_block_reason()
            or "Ứng dụng đang ở chế độ chỉ đọc.",
        )
    from ucg.authz import Action

    if ctx.authz.allows(Action.GRANT):
        return ("Được phép chỉnh sửa", ":material/edit:",
                "Bạn có thể cấp và thu hồi quyền trong phạm vi được cấu hình.")
    return (
        "Chỉ đọc",
        ":material/visibility:",
        f"Vai trò “{ctx.authz.role_label}” không bao gồm quyền thay đổi.",
    )


_CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700&display=swap');

/* Apply font to text elements while preserving icon fonts */
html, body, p, label, h1, h2, h3, h4, input, textarea {
    font-family: 'Be Vietnam Pro', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
}

/* Explicitly protect Material Symbols so ligatures render as icons, not text */
[data-testid="stIconMaterial"], [data-testid="stIconMaterial"] *, 
.material-symbols-rounded, .material-symbols-outlined, [class*="material-symbols"] {
    font-family: 'Material Symbols Rounded', 'Material Symbols Outlined', 'Material Icons' !important;
    font-weight: normal !important;
    font-style: normal !important;
    line-height: 1 !important;
    letter-spacing: normal !important;
    text-transform: none !important;
    display: inline-block !important;
    white-space: nowrap !important;
    word-wrap: normal !important;
    direction: ltr !important;
    -webkit-font-smoothing: antialiased !important;
}

/* Prevent button text from wrapping vertically */
.stButton > button, div[data-testid="stButton"] > button {
    white-space: nowrap !important;
    min-height: 40px !important;
    font-weight: 500 !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 6px !important;
    padding: 0 16px !important;
}

/* Align columns and prevent overflowing */
div[data-testid="column"] {
    min-width: 0 !important;
}

/* Stable selectboxes and text inputs */
div[data-testid="stSelectbox"] label, 
div[data-testid="stTextInput"] label, 
div[data-testid="stMultiSelect"] label {
    white-space: nowrap !important;
    font-weight: 600 !important;
}

/* Radio buttons alignment */
div[data-testid="stRadio"] > div {
    gap: 16px !important;
    flex-wrap: wrap !important;
}

/* Workspace URL wrap */
.workspace-host-caption {
    word-break: break-all;
    font-size: 0.82rem;
}
</style>
"""


def inject_custom_css():
    st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)


def render_banner(ctx: Context):
    """Top-of-page context strip, shown on every screen."""
    inject_custom_css()
    env_label, env_icon = environment_marker(ctx)
    mode_label, mode_icon, mode_why = mode_marker(ctx)

    if ctx.settings.environment in _HIGH_RISK:
        # Words and an icon, not just a colour: a PROD warning must survive a
        # greyscale screenshot and a colour-blind reader.
        st.error(
            f"**{env_label}** — mọi thay đổi ở đây ảnh hưởng tới hệ thống thật.",
            icon=env_icon,
        )

    cols = st.columns([3, 2, 2])
    with cols[0]:
        st.markdown(f"{':material/database:'} **Workspace**")
        st.caption(_host_label(ctx))
    with cols[1]:
        st.markdown(f"{env_icon} **{env_label}**")
        st.caption(f"Phạm vi: {ctx.settings.scope_label}")
    with cols[2]:
        st.markdown(f"{mode_icon} **{mode_label}**")
        st.caption(mode_why)


def render_identity(ctx: Context):
    """The two-identity explanation, in plain words."""
    running_as = running_identity(ctx)
    st.markdown("**Bạn đang đăng nhập**")
    st.caption(f"{ctx.actor.label or 'Chưa xác định'} · vai trò: {ctx.authz.role_label}")
    st.caption(ctx.actor.trust_note)

    st.markdown("**Yêu cầu được thực hiện bằng**")
    label = ExecutionIdentity.LABELS.get(ctx.execution_identity, ctx.execution_identity)
    st.caption(f"{label}{(' · ' + running_as) if running_as else ''}")
    if ctx.execution_identity == ExecutionIdentity.APP_SERVICE_PRINCIPAL:
        st.caption(
            "Bạn đang thao tác; yêu cầu được gửi tới Databricks bằng tài khoản dịch vụ "
            "của ứng dụng. Những gì bạn nhìn thấy là những gì tài khoản dịch vụ đó "
            "được phép nhìn thấy, không phải quyền cá nhân của bạn."
        )


def render_sidebar(ctx: Context):
    with st.sidebar:
        st.markdown("#### Bối cảnh")
        render_identity(ctx)
        st.divider()

        st.markdown("**Phạm vi quản lý**")
        if ctx.settings.catalogs:
            for name in sorted(ctx.settings.catalogs):
                st.caption(f":material/folder: {name}")
        else:
            st.caption("Tất cả catalog mà tài khoản dịch vụ nhìn thấy.")
            if ctx.settings.writes_possible:
                st.warning(
                    "Chưa giới hạn catalog: thao tác ghi áp dụng được cho **mọi** "
                    "catalog mà tài khoản dịch vụ quản lý được.",
                    icon=":material/warning:",
                )

        st.divider()
        if st.button("Làm mới dữ liệu", width="stretch", icon=":material/refresh:",
                     key="ucg_refresh_all"):
            state.clear_caches()
            st.rerun()
        st.caption("Làm mới đọc lại từ Databricks và bỏ mọi bản xem trước đang chờ.")

        pending = state.get_plan()
        if pending is not None:
            st.divider()
            st.info(
                f"Đang có 1 bản xem trước chờ xác nhận trên **{pending.target_name}**.",
                icon=":material/pending_actions:",
            )

        unresolved = ctx.log.needing_reconcile()
        if unresolved:
            st.divider()
            st.warning(
                f"{len(unresolved)} thao tác chưa xác định kết quả. "
                "Hãy mở “Thao tác gần đây” và đối chiếu trước khi thử lại.",
                icon=":material/help:",
            )


def _host_label(ctx: Context) -> str:
    host = (ctx.connection.host or "").replace("https://", "").rstrip("/")
    return host or "Không xác định"


def role_help(ctx: Context):
    st.caption(Role.DESCRIPTIONS.get(ctx.authz.role, ""))
