"""Looking up who a grant can be given to.

The distinction this endpoint preserves is the one the change-access screen
depends on: "nothing matched" and "we were not allowed to look" are different
answers, and only one of them means the principal does not exist. The app
service principal usually sees only itself in workspace SCIM, and workspace SCIM
never sees account groups at all, so a blank result is normal and must not be
reported as absence.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ucg.services.base import Context
from ucg.services.principals import Principal, PrincipalService

from ..http import CTX, fail

router = APIRouter(prefix="/principals", tags=["principals"])

MIN_TERM = 2


def _principal(p: Principal) -> dict:
    return {
        "identifier": p.identifier,
        "kind": p.kind,
        "display_name": p.display_name,
        "label": p.label,
        "type_label": p.type_label,
        "valid_for_uc": p.valid_for_uc,
        "note": p.note,
        "active": p.active,
    }


@router.get("/search")
def search(term: str = Query(default=""), ctx: Context = CTX) -> dict:
    """Search users, groups and service principals by name.

    Returns ``completeness`` so the client can tell the two blank cases apart:
    ``partial_permission`` means the directory could not be read, ``complete``
    means it was read and held nothing matching.
    """
    cleaned = (term or "").strip()
    if len(cleaned) < MIN_TERM:
        return {
            "items": [],
            "completeness": "complete",
            "too_short": True,
            "min_length": MIN_TERM,
            "scope_note": PrincipalService.SCOPE_NOTE,
        }

    service = PrincipalService(ctx)
    listing = service.search(cleaned)
    if not listing.ok:
        raise fail(listing.error)

    usable = [p for p in listing.items if p.valid_for_uc]
    rejected = [p for p in listing.items if not p.valid_for_uc]
    return {
        "items": [_principal(p) for p in usable],
        # Workspace-local groups are surfaced rather than silently dropped, so
        # an operator who searched for one learns why it cannot be used.
        "rejected": [_principal(p) for p in rejected],
        "completeness": listing.completeness,
        "observed_at": listing.observed_at,
        "note": listing.note,
        "too_short": False,
        "min_length": MIN_TERM,
        "scope_note": PrincipalService.SCOPE_NOTE,
    }


@router.get("/describe")
def describe(identifier: str = Query(default=""), ctx: Context = CTX) -> dict:
    """How Unity Catalog will read one typed identifier.

    UC has no principal-type lookup: it interprets the string by shape. Showing
    that reading back is more honest than a directory lookup that would often
    fail for reasons unrelated to whether the grant will work.
    """
    service = PrincipalService(ctx)
    try:
        return _principal(service.describe(identifier))
    except Exception as exc:
        raise fail(exc) from None
