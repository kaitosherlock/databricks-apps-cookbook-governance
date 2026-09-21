"""Builds the backend context for one Streamlit session.

Caching policy, which is a security property here rather than a performance
detail:

* the app service principal's ``WorkspaceClient`` is identity-independent, so it
  is cached process-wide with ``st.cache_resource``;
* **no Unity Catalog data is ever cached process-wide.** The same call returns
  different rows for different identities, so every listing lives in
  ``st.session_state``, which Streamlit isolates per browser session.
"""
from __future__ import annotations

import streamlit as st

from ucg.audit import OperationLog, configure_logging
from ucg.authz import Authorizer
from ucg.capabilities import CapabilityReport
from ucg.clients import (
    Connection, running_in_apps, service_principal_connection, user_connection,
    whoami, workspace_host,
)
from ucg.config import Settings
from ucg.errors import Code, GovernanceError, translate
from ucg.identity import Actor, ExecutionIdentity, cache_key, read_actor, user_token
from ucg.services.base import Context

configure_logging()


@st.cache_resource(show_spinner=False)
def _service_principal_connection(_fingerprint: str) -> Connection:
    """One client for the app identity. Keyed only by deployment config."""
    return service_principal_connection(Settings.from_env())


def settings() -> Settings:
    if "ucg_settings" not in st.session_state:
        st.session_state["ucg_settings"] = Settings.from_env()
    return st.session_state["ucg_settings"]


def operation_log() -> OperationLog:
    if "ucg_oplog" not in st.session_state:
        st.session_state["ucg_oplog"] = OperationLog()
    return st.session_state["ucg_oplog"]


def _headers():
    try:
        return st.context.headers
    except Exception:
        return {}


def actor() -> Actor:
    """The signed-in person, verified against Databricks where possible."""
    cached = st.session_state.get("ucg_actor")
    if isinstance(cached, Actor):
        return cached

    base = read_actor(_headers())
    resolved = base
    token = user_token(_headers())
    if token:
        # A forwarded user token lets us prove the identity rather than trusting
        # a header Databricks makes no anti-spoofing claim about.
        try:
            conn = user_connection(token)
            name = whoami(conn.client)
            if name:
                resolved = Actor(
                    email=name.strip().lower(),
                    display_name=base.display_name or name,
                    verified=True,
                    has_user_token=True,
                    request_id=base.request_id,
                )
        except Exception:
            # Verification is best-effort; we fall back to the header value and
            # keep `verified=False` so the UI does not overstate the guarantee.
            resolved = base
    st.session_state["ucg_actor"] = resolved
    return resolved


def user_view_connection() -> Connection | None:
    """A client acting as the signed-in user, when the app allows it.

    Returns ``None`` when the app has no user-authorization scopes. Callers must
    treat that as "this view is unavailable", never as a reason to substitute
    the service principal.
    """
    token = user_token(_headers())
    if not token:
        return None
    try:
        return user_connection(token)
    except GovernanceError:
        return None


def build_context() -> Context | None:
    """Assemble the request context, or render a setup screen and return None."""
    cfg = settings()
    who = actor()

    if not cfg.local and not running_in_apps() and not who.present:
        _render_no_identity()
        return None

    try:
        connection = _service_principal_connection(_config_fingerprint(cfg))
    except Exception as exc:
        _render_connection_failure(translate(exc))
        return None

    authz = Authorizer(cfg, who.email)

    key = cache_key(who, connection.execution_identity, connection.host)
    if st.session_state.get("ucg_capability_key") != key:
        st.session_state["ucg_capabilities"] = CapabilityReport(cfg)
        st.session_state["ucg_capability_key"] = key
        # A new identity must never see the previous one's data.
        _clear_data_cache()

    return Context(
        settings=cfg,
        actor=who,
        connection=connection,
        authz=authz,
        capabilities=st.session_state["ucg_capabilities"],
        log=operation_log(),
        user_connection=None,
    )


def _config_fingerprint(cfg: Settings) -> str:
    return f"{cfg.local}|{workspace_host()}"


def _clear_data_cache():
    for key in [k for k in st.session_state if str(k).startswith("ucg_data_")]:
        del st.session_state[key]


def running_identity(ctx: Context) -> str:
    """Who the API calls actually run as. Cached per session, not globally."""
    if "ucg_running_as" not in st.session_state:
        st.session_state["ucg_running_as"] = whoami(ctx.w) or ""
    return st.session_state["ucg_running_as"]


# -- fallback screens ----------------------------------------------------
def _render_no_identity():
    st.title("Chưa xác định được người dùng")
    st.error(
        "Ứng dụng không nhận được danh tính từ proxy của Databricks Apps.",
        icon=":material/person_off:",
    )
    st.markdown(
        """
**Nguyên nhân thường gặp**

1. Bạn đang mở ứng dụng bằng một địa chỉ khác URL chính thức của Databricks Apps.
2. Ứng dụng đang chạy ngoài môi trường Databricks Apps.

**Việc cần làm**

- Mở ứng dụng bằng đúng URL Databricks Apps của workspace.
- Nếu bạn đang phát triển trên máy cá nhân, đặt `GOVERNANCE_LOCAL=true`
  để chạy ở chế độ chỉ đọc bằng hồ sơ Databricks CLI.
        """
    )


def _render_connection_failure(err: GovernanceError):
    st.title("Chưa kết nối được Databricks")
    st.error(err.message, icon=":material/cloud_off:")
    if err.next_step:
        st.info(err.next_step)
    if err.code == Code.NOT_CONFIGURED:
        st.markdown(
            "Kiểm tra rằng ứng dụng đang chạy trên Databricks Apps và biến môi trường "
            "`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` được runtime cung cấp."
        )
    st.caption(f"Mã tra cứu: `{err.correlation_id}`")
