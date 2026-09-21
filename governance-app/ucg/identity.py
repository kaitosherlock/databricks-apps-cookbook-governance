"""Who is asking, and who the request will actually run as.

These are two different identities and the app must never blur them:

* the **actor** is the signed-in person, taken from the Databricks Apps proxy;
* the **execution identity** is whoever the SDK call authenticates as - normally
  the app's own service principal.

Databricks does not document the ``X-Forwarded-*`` headers as spoof-proof, so
the actor is treated as display/audit metadata by default. When the app has
user-authorization scopes declared, the forwarded OAuth token lets us *verify*
the actor by calling ``currentUser.me()`` with it; :attr:`Actor.verified` says
which of the two happened, and the UI states it plainly rather than implying a
stronger guarantee than the platform gives.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: Header the Apps proxy injects with the signed-in user's email.
EMAIL_HEADER = "x-forwarded-email"
USER_HEADER = "x-forwarded-user"
USERNAME_HEADER = "x-forwarded-preferred-username"
#: Only present when the app declares user_api_scopes. Never logged, never shown.
TOKEN_HEADER = "x-forwarded-access-token"
REQUEST_ID_HEADER = "x-request-id"


class ExecutionIdentity:
    APP_SERVICE_PRINCIPAL = "app_service_principal"
    LOCAL_PROFILE = "local_profile"
    END_USER = "end_user"

    LABELS = {
        APP_SERVICE_PRINCIPAL: "Tài khoản dịch vụ của ứng dụng",
        LOCAL_PROFILE: "Hồ sơ Databricks CLI trên máy phát triển",
        END_USER: "Chính tài khoản của bạn (uỷ quyền người dùng)",
    }


@dataclass(frozen=True)
class Actor:
    """The signed-in person."""

    email: str = ""
    display_name: str = ""
    #: True only when the identity was confirmed by calling Databricks with the
    #: user's own forwarded token, not merely read from a proxy header.
    verified: bool = False
    #: Present only when the app declares user-authorization scopes.
    has_user_token: bool = False
    request_id: str = ""

    @property
    def present(self) -> bool:
        return bool(self.email)

    @property
    def label(self) -> str:
        return self.display_name or self.email or "(chưa xác định)"

    @property
    def trust_note(self) -> str:
        if self.verified:
            return "Danh tính đã được Databricks xác minh bằng token của chính bạn."
        return (
            "Danh tính lấy từ header của proxy Databricks Apps. Chỉ tin cậy khi ứng dụng "
            "được truy cập qua đúng URL Databricks Apps."
        )


def _lower_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    out = {}
    for key, value in headers.items():
        try:
            out[str(key).lower()] = value
        except Exception:
            continue
    return out


def read_actor(headers: Mapping[str, str] | None) -> Actor:
    """Build an unverified actor from the Apps proxy headers."""
    lowered = _lower_headers(headers)
    email = (lowered.get(EMAIL_HEADER) or "").strip().lower()
    return Actor(
        email=email,
        display_name=(lowered.get(USERNAME_HEADER) or lowered.get(USER_HEADER) or "").strip(),
        verified=False,
        has_user_token=bool((lowered.get(TOKEN_HEADER) or "").strip()),
        request_id=(lowered.get(REQUEST_ID_HEADER) or "").strip(),
    )


def user_token(headers: Mapping[str, str] | None) -> str:
    """The forwarded user OAuth token, or an empty string.

    The value is a live credential. It is returned only to be handed straight to
    a ``WorkspaceClient``; it is never logged, cached, or rendered.
    """
    return (_lower_headers(headers).get(TOKEN_HEADER) or "").strip()


def cache_key(actor: Actor, execution_identity: str, host: str = "") -> str:
    """Isolation key for anything cached per identity.

    Caches that hold Unity Catalog metadata must never be shared between two
    signed-in people or between the service-principal and on-behalf-of views:
    the same call returns different rows for different identities.
    """
    return "|".join((host or "-", execution_identity, actor.email or "-"))
