"""Process-level stores that replace Streamlit's session state.

Streamlit gave three things for free that an HTTP server has to provide
explicitly, and each one is a correctness property rather than a convenience:

* ``st.cache_resource`` held one ``WorkspaceClient`` for the app identity. That
  client is identity-independent, so it is cached process-wide here too.
* ``st.session_state`` isolated Unity Catalog data per browser session. The same
  call returns different rows for different identities, so everything cached
  here is keyed by :func:`ucg.identity.cache_key` - never shared across actors.
* ``st.session_state`` also held the pending ``Plan``. It stays server-side for
  the same reason it did before: a preview the browser could edit is not a
  preview, it is a suggestion.

Every store is bounded and guarded by a lock, because an ASGI server handles
requests concurrently where a Streamlit script did not.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime, timezone

from ucg.audit import OperationLog
from ucg.capabilities import CapabilityReport
from ucg.clients import Connection, service_principal_connection, workspace_host
from ucg.config import Settings
from ucg.plans import Plan

_LOCK = threading.RLock()

#: One client for the app's own identity, rebuilt only if deployment config changes.
_CONNECTION: dict[str, Connection] = {}
#: cache_key -> CapabilityReport. Probes are expensive and identity-dependent.
_CAPABILITIES: "OrderedDict[str, CapabilityReport]" = OrderedDict()
#: actor email -> OperationLog. The log is what the "recent activity" screen reads.
_LOGS: "OrderedDict[str, OperationLog]" = OrderedDict()
#: plan_id -> Plan. Opaque to the client; see the module docstring.
_PLANS: "OrderedDict[str, Plan]" = OrderedDict()

#: Ceilings. A governance app has few concurrent operators; these are generous.
MAX_IDENTITIES = 64
MAX_PLANS = 256


def settings() -> Settings:
    """Read deployment config. Cheap, and always reflects the current process."""
    return Settings.from_env()


def config_fingerprint(cfg: Settings) -> str:
    return f"{cfg.local}|{workspace_host()}"


def connection(cfg: Settings) -> Connection:
    """The app service principal's client, built once per deployment config."""
    key = config_fingerprint(cfg)
    with _LOCK:
        hit = _CONNECTION.get(key)
        if hit is not None:
            return hit
    # Built outside the lock: constructing a client performs network I/O and
    # must not block every other request while it happens.
    built = service_principal_connection(cfg)
    with _LOCK:
        return _CONNECTION.setdefault(key, built)


def capabilities(cache_key: str, cfg: Settings) -> CapabilityReport:
    """Capability report for one identity, created on first use."""
    with _LOCK:
        hit = _CAPABILITIES.get(cache_key)
        if hit is not None:
            _CAPABILITIES.move_to_end(cache_key)
            return hit
        report = CapabilityReport(cfg)
        _CAPABILITIES[cache_key] = report
        while len(_CAPABILITIES) > MAX_IDENTITIES:
            _CAPABILITIES.popitem(last=False)
        return report


def reset_capabilities(cache_key: str):
    """Drop a probe report so the next request re-probes."""
    with _LOCK:
        _CAPABILITIES.pop(cache_key, None)


def operation_log(actor_email: str) -> OperationLog:
    """The audit log for one operator.

    Keyed by actor so one person's activity screen never shows another's, and
    kept in the process so it survives a page reload - which the Streamlit
    version did not.
    """
    key = (actor_email or "-").strip().lower()
    with _LOCK:
        hit = _LOGS.get(key)
        if hit is not None:
            _LOGS.move_to_end(key)
            return hit
        log = OperationLog()
        _LOGS[key] = log
        while len(_LOGS) > MAX_IDENTITIES:
            _LOGS.popitem(last=False)
        return log


# -- plan store -----------------------------------------------------------
def put_plan(plan: Plan) -> str:
    """Store a preview and return the opaque handle the client will hold."""
    with _LOCK:
        _prune_plans()
        _PLANS[plan.plan_id] = plan
        while len(_PLANS) > MAX_PLANS:
            _PLANS.popitem(last=False)
    return plan.plan_id


def get_plan(plan_id: str) -> Plan | None:
    """Fetch a stored preview. Expired and spent plans are never returned."""
    with _LOCK:
        _prune_plans()
        plan = _PLANS.get((plan_id or "").strip())
        if plan is None:
            return None
        if plan.expired or plan.consumed:
            _PLANS.pop(plan.plan_id, None)
            return None
        return plan


def drop_plan(plan_id: str):
    with _LOCK:
        _PLANS.pop((plan_id or "").strip(), None)


def drop_plans_for(actor_email: str):
    """Discard every pending preview belonging to one operator."""
    actor = (actor_email or "").strip().lower()
    with _LOCK:
        for plan_id in [k for k, p in _PLANS.items() if p.actor == actor]:
            _PLANS.pop(plan_id, None)


def pending_plans(actor_email: str) -> list[Plan]:
    actor = (actor_email or "").strip().lower()
    with _LOCK:
        _prune_plans()
        return [p for p in _PLANS.values() if p.actor == actor]


def _prune_plans():
    """Caller holds the lock."""
    for plan_id in [k for k, p in _PLANS.items() if p.expired or p.consumed]:
        _PLANS.pop(plan_id, None)


def started_at() -> str:
    return _STARTED.strftime("%Y-%m-%d %H:%M:%S UTC")


_STARTED = datetime.now(timezone.utc)
