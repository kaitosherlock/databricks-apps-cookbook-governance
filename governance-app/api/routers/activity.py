"""What this app did, and what it is not sure it did.

The operation log is the app own record, distinct from the Databricks audit log.
It exists mainly so an operator can answer one question after a timeout: did my
change go through? Events whose outcome is unknown are surfaced separately and
never folded into a success count.
"""
from __future__ import annotations

from fastapi import APIRouter

from ucg.audit import Event, OperationLog, Status
from ucg.services.base import Context

from ..http import CTX

router = APIRouter(prefix="/activity", tags=["activity"])


def _event(event: Event) -> dict:
    return {
        "event_id": event.event_id,
        "operation_id": event.operation_id,
        "time_utc": event.time_utc,
        "actor": event.actor,
        "execution_identity": event.execution_identity,
        "action": event.action,
        "summary": event.summary or event.action,
        "target": event.target,
        "target_type": event.target_type,
        "principal": event.principal,
        "reason": event.reason,
        "status": event.status,
        "status_label": event.status_label,
        "needs_reconcile": event.status in Status.NEEDS_RECONCILE,
        "error_code": event.error_code,
        "correlation_id": event.correlation_id,
        "details": event.details,
    }


@router.get("")
def recent(ctx: Context = CTX) -> dict:
    events = ctx.log.events()
    unresolved = ctx.log.needing_reconcile()
    return {
        "items": [_event(e) for e in events],
        "unresolved": [_event(e) for e in unresolved],
        "disclaimer": OperationLog.DISCLAIMER,
        "statuses": Status.LABELS,
    }
