"""Turning backend outcomes into HTTP, and nothing else.

``GovernanceError`` already carries everything a user needs: a stable code, a
Vietnamese message, a next step and a correlation id. This module maps the code
onto a status and hands the rest through untouched. It never invents a message
and never lets an SDK exception reach the wire, because SDK exceptions can
carry response bodies and response bodies can carry credentials.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from ucg.errors import Code, GovernanceError, translate
from ucg.services.base import Context

from . import context as ctx_module

#: Stable code -> HTTP status. The client branches on ``code``, not on status,
#: so this mapping is for well-behaved intermediaries rather than for the UI.
STATUS = {
    Code.INVALID_INPUT: 400,
    Code.INVALID_NAME: 400,
    Code.INVALID_PRIVILEGE: 400,
    Code.NOT_FOUND: 404,
    Code.ALREADY_SATISFIED: 409,

    Code.NO_IDENTITY: 401,
    Code.FORBIDDEN_ROLE: 403,
    Code.FORBIDDEN_SCOPE: 403,
    Code.WRITES_DISABLED: 403,
    Code.SELF_APPROVAL: 403,
    Code.PERMISSION_DENIED: 403,

    Code.NOT_CONFIGURED: 409,
    Code.CAPABILITY_UNAVAILABLE: 409,
    Code.WAREHOUSE_REQUIRED: 409,
    Code.STALE_PLAN: 409,
    Code.CONFLICT: 409,

    Code.TIMEOUT_UNKNOWN: 504,
    Code.UPSTREAM_ERROR: 502,
    Code.INTERNAL: 500,
}


class ApiError(HTTPException):
    """A GovernanceError on its way out as JSON."""

    def __init__(self, err: GovernanceError):
        super().__init__(
            status_code=STATUS.get(err.code, 500),
            detail={"error": err.as_dict()},
        )
        self.governance_error = err


def fail(exc: BaseException) -> ApiError:
    """Normalise anything into an ApiError. Never leaks an upstream body."""
    return ApiError(translate(exc))


def get_context(request: Request) -> Context:
    """FastAPI dependency: one backend context per request."""
    try:
        return ctx_module.build(request.headers)
    except ctx_module.NoIdentity:
        raise ApiError(GovernanceError(
            Code.NO_IDENTITY,
            "Ứng dụng không nhận được danh tính từ proxy của Databricks Apps.",
            detail="missing x-forwarded-email",
        )) from None
    except GovernanceError as err:
        raise ApiError(err) from None
    except Exception as exc:
        raise fail(exc) from None


#: Convenience alias so routers read as ``ctx: Context = CTX``.
CTX = Depends(get_context)


def listing_payload(listing, items) -> dict:
    """The honesty fields travel with every list, exactly as in the backend.

    ``completeness`` and ``observed_at`` are the difference between "there is
    nothing here" and "this is what we were allowed to see", and the UI renders
    those two states differently. Dropping them at the transport layer would
    quietly turn one into the other.
    """
    return {
        "items": items,
        "completeness": getattr(listing, "completeness", "complete"),
        "observed_at": getattr(listing, "observed_at", ""),
        "note": getattr(listing, "note", ""),
    }


def require_ok(listing):
    """Raise a listing's stored error, if it has one."""
    if listing is not None and not listing.ok:
        raise ApiError(listing.error)
    return listing
