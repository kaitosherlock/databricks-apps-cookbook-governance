"""What this deployment can actually do, and why anything is switched off.

A capability is never reported as simply "unavailable". The distinction between
"not configured", "no permission", "workspace does not support it" and "we have
not checked" is the whole value of this screen: only the second one is fixed by
granting something, and only the first is fixed by editing app.yaml.
"""
from __future__ import annotations

from fastapi import APIRouter

from ucg import probes
from ucg.capabilities import Capability, State
from ucg.services.base import Context

from .. import context as ctx_module
from .. import runtime
from ..http import CTX

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


def _capability(cap: Capability) -> dict:
    return {
        "key": cap.key,
        "name": cap.name,
        "group": cap.group,
        "api": cap.api,
        "state": cap.state,
        "state_label": cap.state_label,
        "detail": cap.detail,
        "usable": cap.usable,
        "maturity": cap.maturity,
        "needs_warehouse": cap.needs_warehouse,
        "needs_account": cap.needs_account,
    }


@router.get("")
def read_diagnostics(ctx: Context = CTX) -> dict:
    report = ctx.capabilities
    items = sorted(report.items.values(), key=lambda c: (c.group, c.name))

    groups: dict[str, list[dict]] = {}
    for cap in items:
        groups.setdefault(cap.group or "Khác", []).append(_capability(cap))

    return {
        "capabilities": [_capability(c) for c in items],
        "groups": [
            {"group": name, "items": caps} for name, caps in sorted(groups.items())
        ],
        "summary": report.summary(),
        "state_labels": State.LABELS,
        "config": ctx.settings.describe(),
        "runtime": {
            "started_at": runtime.started_at(),
            "execution_identity": ctx.execution_identity,
            "host": ctx.connection.host,
            "warehouse_id": ctx.settings.warehouse_id,
        },
    }


@router.post("/probe")
def run_probes(include_sql: bool = False, ctx: Context = CTX) -> dict:
    """Actually call Databricks to find out, instead of reporting "unchecked".

    Probes are read-only. The SQL-backed set is opt-in because it spends
    warehouse time, which a diagnostics page should not do without being asked.
    """
    report = ctx.capabilities
    result = probes.run(report, probes.cheap_probes(ctx))
    sql_result = None
    if include_sql and ctx.settings.warehouse_id:
        sql_result = probes.run(report, probes.sql_probes(ctx))

    return {
        "ran": result.ran + (sql_result.ran if sql_result else 0),
        "usable": result.usable + (sql_result.usable if sql_result else 0),
        "skipped": list(result.skipped) + (list(sql_result.skipped) if sql_result else []),
        "sql_probed": sql_result is not None,
        "summary": report.summary(),
    }


@router.post("/reset")
def reset(ctx: Context = CTX) -> dict:
    """Throw away probe results so the next read starts from the static rules."""
    runtime.reset_capabilities(ctx_module.identity_key(ctx))
    return {"ok": True}
