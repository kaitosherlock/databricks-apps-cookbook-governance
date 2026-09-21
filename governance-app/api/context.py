"""Build one :class:`ucg.services.base.Context` per HTTP request.

The actor is re-derived from the Databricks Apps proxy headers on *every*
request. There is deliberately no session cookie, no login endpoint and no
client-supplied identity: the proxy is the only thing that can say who is
asking, so there is nothing for a forged request to forge.

Identity verification (calling Databricks with the user's own forwarded token)
is a network round trip, so a positive result is cached briefly against a hash
of the token. The token itself is never stored, logged or returned.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Mapping

from ucg.authz import Authorizer
from ucg.clients import running_in_apps, user_connection, whoami
from ucg.errors import Code, GovernanceError
from ucg.identity import Actor, cache_key, read_actor, user_token
from ucg.services.base import Context

from . import runtime

#: How long a verified identity is trusted before we ask Databricks again.
VERIFY_TTL_SECONDS = 300
MAX_VERIFIED = 256

_LOCK = threading.RLock()
#: sha256(token) -> (expires_at, resolved email, display name)
_VERIFIED: "OrderedDict[str, tuple[float, str, str]]" = OrderedDict()


class NoIdentity(Exception):
    """The proxy did not supply an identity and this deployment requires one."""


def _token_fingerprint(token: str) -> str:
    """A stable handle for a credential we must never keep."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cached_verification(fingerprint: str) -> tuple[str, str] | None:
    now = time.monotonic()
    with _LOCK:
        hit = _VERIFIED.get(fingerprint)
        if hit is None:
            return None
        expires, email, display = hit
        if expires < now:
            _VERIFIED.pop(fingerprint, None)
            return None
        _VERIFIED.move_to_end(fingerprint)
        return email, display


def _remember_verification(fingerprint: str, email: str, display: str):
    with _LOCK:
        _VERIFIED[fingerprint] = (time.monotonic() + VERIFY_TTL_SECONDS, email, display)
        while len(_VERIFIED) > MAX_VERIFIED:
            _VERIFIED.popitem(last=False)


def resolve_actor(headers: Mapping[str, str]) -> Actor:
    """The signed-in person, verified against Databricks when possible."""
    base = read_actor(headers)
    token = user_token(headers)
    if not token:
        return base

    fingerprint = _token_fingerprint(token)
    cached = _cached_verification(fingerprint)
    if cached is not None:
        email, display = cached
        return Actor(
            email=email,
            display_name=base.display_name or display,
            verified=True,
            has_user_token=True,
            request_id=base.request_id,
        )

    try:
        name = whoami(user_connection(token).client)
    except Exception:
        # Verification is best-effort. Falling back to the header value with
        # verified=False keeps the UI honest about which guarantee it has.
        return base
    if not name:
        return base

    email = name.strip().lower()
    _remember_verification(fingerprint, email, name)
    return Actor(
        email=email,
        display_name=base.display_name or name,
        verified=True,
        has_user_token=True,
        request_id=base.request_id,
    )


def build(headers: Mapping[str, str]) -> Context:
    """Assemble the request context, or raise something the API can render."""
    cfg = runtime.settings()
    actor = resolve_actor(headers)

    if not cfg.local and not running_in_apps() and not actor.present:
        raise NoIdentity()

    conn = runtime.connection(cfg)
    key = cache_key(actor, conn.execution_identity, conn.host)

    return Context(
        settings=cfg,
        actor=actor,
        connection=conn,
        authz=Authorizer(cfg, actor.email),
        capabilities=runtime.capabilities(key, cfg),
        log=runtime.operation_log(actor.email),
        # The on-behalf-of connection is built only where a screen explicitly
        # asks for the user's own view. It is never a fallback for the service
        # principal, which usually holds wider privilege.
        user_connection=None,
    )


def identity_key(ctx: Context) -> str:
    return cache_key(ctx.actor, ctx.execution_identity, ctx.connection.host)


def require_user_connection(headers: Mapping[str, str]):
    """The signed-in user's own client, or a precise refusal."""
    token = user_token(headers)
    if not token:
        raise GovernanceError(
            Code.NOT_CONFIGURED,
            "Ứng dụng chưa bật uỷ quyền người dùng nên không thể xem bằng quyền của bạn.",
            detail="missing x-forwarded-access-token",
        )
    return user_connection(token)
