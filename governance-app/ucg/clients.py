"""Databricks client construction.

Two rules from the brief are enforced structurally here rather than by
convention:

* **No silent fallback.** If a caller asks for the end-user identity and that
  fails, we raise. We never quietly retry as the app's service principal, which
  usually holds *more* privilege than the user.
* **No client-supplied hosts.** The workspace host always comes from the runtime
  environment, never from anything a browser sent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .config import Settings
from .errors import Code, GovernanceError, translate
from .identity import ExecutionIdentity


@dataclass(frozen=True)
class Connection:
    """A constructed client plus the facts the UI has to show about it."""

    client: object
    execution_identity: str
    host: str = ""
    #: Service principal / user identifier the API calls run as, when known.
    running_as: str = ""

    @property
    def identity_label(self) -> str:
        return ExecutionIdentity.LABELS.get(self.execution_identity, self.execution_identity)


def workspace_host() -> str:
    """The workspace URL, from the runtime only."""
    for name in ("DATABRICKS_HOST", "DATABRICKS_WORKSPACE_URL"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def app_name() -> str:
    return (os.getenv("DATABRICKS_APP_NAME") or "").strip()


def running_in_apps() -> bool:
    """Whether we are inside the Databricks Apps runtime."""
    return bool(os.getenv("DATABRICKS_APP_NAME") or os.getenv("DATABRICKS_APP_PORT"))


def service_principal_connection(settings: Settings) -> Connection:
    """Client for the app's own identity (or a local CLI profile)."""
    from databricks.sdk import WorkspaceClient

    try:
        if settings.local:
            client = WorkspaceClient()
            identity = ExecutionIdentity.LOCAL_PROFILE
        else:
            # oauth-m2m consumes the injected DATABRICKS_CLIENT_ID/SECRET. It is
            # pinned so a stray PAT in the environment cannot change who we are.
            client = WorkspaceClient(auth_type="oauth-m2m")
            identity = ExecutionIdentity.APP_SERVICE_PRINCIPAL
    except Exception as exc:
        raise translate(exc) from None

    return Connection(
        client=client,
        execution_identity=identity,
        host=getattr(getattr(client, "config", None), "host", "") or workspace_host(),
    )


def user_connection(token: str) -> Connection:
    """Client that acts as the signed-in user, using their forwarded token.

    Raises rather than degrading: a caller that wanted the user's own view must
    not silently receive the service principal's wider one.
    """
    if not token:
        raise GovernanceError(
            Code.NOT_CONFIGURED,
            "Ứng dụng chưa bật uỷ quyền người dùng nên không thể xem bằng quyền của bạn.",
            detail="missing x-forwarded-access-token",
        )
    from databricks.sdk import WorkspaceClient

    host = workspace_host()
    if not host:
        raise GovernanceError(
            Code.NOT_CONFIGURED,
            "Không xác định được địa chỉ workspace từ môi trường chạy.",
        )
    try:
        client = WorkspaceClient(host=host, token=token, auth_type="pat")
    except Exception as exc:
        raise translate(exc) from None
    return Connection(client=client, execution_identity=ExecutionIdentity.END_USER, host=host)


def account_connection():
    """Account-level client, when the deployment has been given account credentials.

    A Databricks App's injected credentials are workspace-scoped, so this is
    absent in the default deployment. Callers must treat ``None`` as "account
    features unavailable", never as an error to paper over.
    """
    account_id = (os.getenv("DATABRICKS_ACCOUNT_ID") or "").strip()
    account_host = (os.getenv("DATABRICKS_ACCOUNT_HOST") or "").strip()
    if not account_id or not account_host:
        return None
    from databricks.sdk import AccountClient

    try:
        return AccountClient(host=account_host, account_id=account_id)
    except Exception:
        # Absence of account access is a normal, reportable state.
        return None


def whoami(client) -> str:
    """Identifier the API calls actually run as, for the identity banner."""
    try:
        me = client.current_user.me()
    except Exception:
        return ""
    return (getattr(me, "user_name", "") or getattr(me, "display_name", "") or "").strip()
