"""Reading privileges, and changing them through the plan pipeline.

The security-critical part of this module is what it refuses to accept from the
client. ``/plan`` takes the operator inputs and returns an opaque ``plan_id``;
``/apply`` takes that id plus the typed confirmation and **nothing else**. The
principal, the privileges and the target are all read back from the server copy
of the plan.

That is a stronger guarantee than the Streamlit build had. There, a plan lived
in session state next to widget values and was kept honest by recomputing a
signature over the form on every rerun. Here the plan is simply unreachable from
the browser, so there is no signature to keep in step.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ucg import privileges as priv
from ucg.authz import Action
from ucg.errors import Code, GovernanceError
from ucg.naming import Target
from ucg.plans import Plan
from ucg.services.assets import AssetService
from ucg.services.base import Context
from ucg.services.grants import (
    DIRECT, INHERITED, OWNERSHIP, GrantRow, GrantService, GrantView,
    classify_principal,
)

from .. import runtime
from ..http import CTX, ApiError, fail

router = APIRouter(prefix="/grants", tags=["grants"])

GRANTABLE_KINDS = ("catalog", "schema", "table", "volume", "function", "model")


def _target(kind: str, catalog: str, schema: str, name: str) -> Target:
    if kind not in GRANTABLE_KINDS:
        raise ApiError(GovernanceError(
            Code.INVALID_INPUT, f"Loại đối tượng không hợp lệ: {kind!r}."
        ))
    try:
        if kind == "catalog":
            return Target("catalog", catalog)
        if kind == "schema":
            return Target("schema", catalog, schema)
        return Target(kind, catalog, schema, name)
    except Exception as exc:
        raise fail(exc) from None


def _row(row: GrantRow) -> dict:
    """One grant, with provenance kept explicit.

    ``source`` is the field the whole screen turns on: an inherited grant cannot
    be revoked here, and the UI must not offer a button that would fail.
    """
    readable = not row.unreadable
    info = priv.info(row.privilege)
    return {
        "principal": row.principal,
        "principal_type": row.principal_type or classify_principal(row.principal),
        "privilege": row.privilege if readable else "",
        "privilege_label": info.label if readable else "Không đọc được mã quyền",
        "risk": info.risk if readable else "medium",
        "source": row.source,
        "source_label": row.source_label,
        "inherited_from": row.inherited_from,
        "inherited_from_type": row.inherited_from_type,
        "granted_at": row.inherited_from or (
            "Chính đối tượng này" if row.source == DIRECT else ""
        ),
        "revocable_here": row.revocable_here,
        "unreadable": row.unreadable,
    }


def _view(view: GrantView) -> dict:
    return {
        "rows": [_row(r) for r in view.rows],
        "owner": view.owner,
        "completeness": view.completeness,
        "observed_at": view.observed_at,
        "has_unreadable": view.has_unreadable,
        "caveat": GrantView.CAVEAT,
        "unreadable_note": GrantView.UNREADABLE_NOTE if view.has_unreadable else "",
        "sources": {"direct": DIRECT, "inherited": INHERITED, "ownership": OWNERSHIP},
    }


def _owner_of(service: AssetService, target: Target) -> str:
    try:
        return service.detail(target).owner or ""
    except Exception:
        # The grant list is still worth showing without an owner row; the UI
        # renders an explicit "unknown" rather than implying there is no owner.
        return ""


@router.get("")
def read_grants(
    kind: str = "table",
    catalog: str = "",
    schema: str = "",
    name: str = "",
    effective: bool = True,
    ctx: Context = CTX,
) -> dict:
    """Privileges on one object.

    ``effective=true`` resolves inheritance; ``false`` returns direct grants
    only. Both are offered because building an access picture from the direct
    endpoint alone is the classic Unity Catalog mistake.
    """
    target = _target(kind, catalog, schema, name)
    grants = GrantService(ctx)
    assets = AssetService(ctx)

    view = grants.effective(target) if effective else grants.direct(target)
    if not view.ok:
        raise ApiError(view.error)
    grants.with_owner(view, _owner_of(assets, target))
    return _view(view)


@router.get("/privileges")
def available(
    kind: str = "table",
    catalog: str = "",
    schema: str = "",
    name: str = "",
    ctx: Context = CTX,
) -> dict:
    """Privileges that are valid on this object, with their meaning and risk."""
    target = _target(kind, catalog, schema, name)
    grants = GrantService(ctx)
    assets = AssetService(ctx)

    table_type = ""
    if target.kind == "table":
        try:
            table_type = assets.detail(target).sub_type
        except Exception:
            table_type = ""

    try:
        codes = grants.available_privileges(target, table_type=table_type)
    except Exception as exc:
        raise fail(exc) from None

    return {
        "privileges": [
            {
                "code": p.code,
                "label": p.label,
                "display": p.display,
                "explanation": p.explanation,
                "risk": p.risk,
            }
            for p in (priv.info(c) for c in codes)
        ],
        "traversal_note": priv.traversal_note(target.kind),
        "inheritance_note": priv.inheritance_note(target.kind),
    }


# -- the mutation pipeline -------------------------------------------------
class PlanRequest(BaseModel):
    """What the operator chose. Every field is re-validated by the backend.

    ``schema_name`` carries the wire name ``schema`` through an alias: a field
    literally called ``schema`` shadows a BaseModel attribute in Pydantic v2 and
    is removed in v3, but the JSON key is part of the API contract.
    """

    model_config = ConfigDict(populate_by_name=True)

    kind: str
    catalog: str = ""
    schema_name: str = Field(default="", alias="schema")
    name: str = ""
    action: str = Field(pattern="^(grant|revoke)$")
    principal: str
    privileges: list[str]
    reason: str


class ApplyRequest(BaseModel):
    """Everything ``/apply`` is willing to hear from a browser.

    Deliberately minimal: the change itself comes from the stored plan, so a
    tampered client can confirm a plan or fail to confirm it, and nothing else.
    """

    plan_id: str
    confirmation: str


def _plan_payload(plan: Plan) -> dict:
    """Display-only projection of a plan. Enough to review, not enough to forge."""
    return {
        "plan_id": plan.plan_id,
        "operation_id": plan.operation_id,
        "summary": plan.summary,
        "reason": plan.reason,
        "target_name": plan.target_name,
        "target_type": plan.target_type,
        "action": plan.action,
        "created_at": plan.created_at.isoformat(),
        "age_seconds": plan.age_seconds,
        "preview": [{"text": line.text, "kind": line.kind} for line in plan.preview],
        # What the operator must type to confirm. Sent so the client can
        # validate before submitting; the server checks it again regardless.
        "confirm_with": plan.target_name,
    }


def _target_from_plan(plan: Plan) -> Target:
    """Rebuild the target from the *plan*, never from the request.

    ``target_key`` is ``kind:full_name`` and was written by the server when the
    plan was built, so this cannot be steered by a client.
    """
    kind, _, full_name = plan.target_key.partition(":")
    try:
        return Target.parse(kind, full_name)
    except Exception as exc:
        raise fail(exc) from None


@router.post("/plan")
def build_plan(body: PlanRequest, ctx: Context = CTX) -> dict:
    """Validate, authorise and preview. Nothing is sent to Databricks."""
    target = _target(body.kind, body.catalog, body.schema_name, body.name)
    grants = GrantService(ctx)
    assets = AssetService(ctx)

    table_type = ""
    if target.kind == "table":
        try:
            table_type = assets.detail(target).sub_type
        except Exception:
            table_type = ""

    try:
        plan = grants.plan_change(
            target, body.principal, body.action, list(body.privileges),
            body.reason, table_type=table_type,
        )
    except Exception as exc:
        raise fail(exc) from None

    # One pending preview per operator, matching the old sidebar promise that
    # exactly one change is ever waiting for confirmation.
    runtime.drop_plans_for(ctx.actor.email)
    runtime.put_plan(plan)
    return _plan_payload(plan)


@router.delete("/plan/{plan_id}")
def cancel_plan(plan_id: str, ctx: Context = CTX) -> dict:
    plan = runtime.get_plan(plan_id)
    # Only the owner of a plan may discard it.
    if plan is not None and plan.actor == ctx.actor.email:
        runtime.drop_plan(plan_id)
    return {"ok": True}


@router.post("/apply")
def apply_plan(body: ApplyRequest, ctx: Context = CTX) -> dict:
    """Confirm and execute a stored preview.

    The plan carries its own binding checks - actor, execution identity, target,
    age, single-use - and ``Service.execute`` re-authorises and re-reads state
    before anything is sent. This endpoint adds no shortcuts to that path.
    """
    plan = runtime.get_plan(body.plan_id)
    if plan is None:
        raise ApiError(GovernanceError(
            Code.STALE_PLAN,
            "Bản xem trước không còn hiệu lực. Hãy tạo lại để làm việc "
            "trên trạng thái mới nhất.",
        ))
    # Binding is enforced again inside plan.require_fresh; this check exists so
    # one operator can never even address another operator plan by id.
    if plan.actor != ctx.actor.email:
        raise ApiError(GovernanceError(
            Code.STALE_PLAN,
            "Bản xem trước thuộc về ngữ cảnh khác (người dùng hoặc đối tượng đã đổi).",
        ))

    target = _target_from_plan(plan)
    grants = GrantService(ctx)
    try:
        outcome = grants.apply(plan, target, body.confirmation)
    except Exception as exc:
        runtime.drop_plan(plan.plan_id)
        raise fail(exc) from None

    runtime.drop_plan(plan.plan_id)
    return {
        "status": outcome.status,
        "headline": outcome.headline(),
        "event_id": outcome.event_id,
        "operation_id": outcome.operation_id,
        "message": outcome.message,
        "succeeded": outcome.succeeded,
        "unknown": outcome.unknown,
        "verified": outcome.verified_state,
        "summary": plan.summary,
        "principal": plan.payload.get("principal", ""),
        "privileges": plan.payload.get("privileges", []),
        "target": plan.target_name,
        "operation": plan.payload.get("operation", ""),
        "error": outcome.error.as_dict() if outcome.error else None,
    }


@router.get("/revocable")
def revocable(
    kind: str = "table",
    catalog: str = "",
    schema: str = "",
    name: str = "",
    ctx: Context = CTX,
) -> dict:
    """Direct grants that could actually be revoked here, grouped by principal.

    Inherited grants are excluded on purpose: they cannot be revoked at this
    object, and offering them would be offering a button that fails.
    """
    target = _target(kind, catalog, schema, name)
    grants = GrantService(ctx)
    view = grants.direct(target)
    if not view.ok:
        raise ApiError(view.error)

    by_principal: dict[str, list[str]] = {}
    for row in view.rows:
        if row.revocable_here:
            by_principal.setdefault(row.principal, []).append(row.privilege)

    return {
        "can_revoke": ctx.authz.allows(Action.REVOKE, target),
        "reason": ctx.authz.check(Action.REVOKE, target).reason,
        "principals": [
            {
                "principal": p,
                "principal_type": classify_principal(p),
                "privileges": sorted(set(codes)),
            }
            for p, codes in sorted(by_principal.items())
        ],
        "observed_at": view.observed_at,
    }
