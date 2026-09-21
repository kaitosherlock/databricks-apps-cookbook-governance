"""Session state: the selected object, the filters, and the pending plan.

Two behaviours the brief calls out specifically:

* the chosen catalog/schema/object and the search filters survive navigation,
  so moving between "Thông tin" and "Quyền truy cập" never makes the user pick
  the object again;
* a pending preview is invalidated the moment anything material changes. In
  Streamlit every button press is a rerun, so without that rule a stale preview
  is one stray click away from being applied.
"""
from __future__ import annotations

import streamlit as st

from ucg.naming import Target
from ucg.plans import Plan

SELECTION = "ucg_selection"
PLAN = "ucg_plan"
PLAN_SIGNATURE = "ucg_plan_signature"
NOTICE = "ucg_notice"
FILTERS = "ucg_filters"
BUSY = "ucg_busy"


# -- selection ----------------------------------------------------------
def selection() -> dict:
    if SELECTION not in st.session_state:
        st.session_state[SELECTION] = {
            "catalog": "", "schema": "", "kind": "table", "name": "",
        }
    return st.session_state[SELECTION]


def set_selection(**changes):
    current = selection()
    changed = False
    for key, value in changes.items():
        if current.get(key) != value:
            current[key] = value
            changed = True
    if changed:
        # Navigating to a different object must not carry a preview with it.
        clear_plan()
    st.session_state[SELECTION] = current


def select_target(target: Target):
    set_selection(
        catalog=target.catalog, schema=target.schema,
        kind=target.kind, name=target.name,
    )


def current_target() -> Target | None:
    sel = selection()
    kind = sel.get("kind") or ""
    try:
        if kind == "catalog" and sel.get("catalog"):
            return Target("catalog", sel["catalog"])
        if kind == "schema" and sel.get("catalog") and sel.get("schema"):
            return Target("schema", sel["catalog"], sel["schema"])
        if kind in ("table", "volume", "function", "model") and sel.get("name"):
            return Target(kind, sel["catalog"], sel["schema"], sel["name"])
    except Exception:
        return None
    return None


def has_object() -> bool:
    return current_target() is not None


# -- filters -------------------------------------------------------------
def filters() -> dict:
    if FILTERS not in st.session_state:
        st.session_state[FILTERS] = {
            "search": "", "kinds": ["table"], "principal": "",
            "privilege": "", "source": "Tất cả",
        }
    return st.session_state[FILTERS]


def set_filter(key: str, value):
    filters()[key] = value


# -- pending plan --------------------------------------------------------
def set_plan(plan: Plan, signature: str):
    st.session_state[PLAN] = plan
    st.session_state[PLAN_SIGNATURE] = signature


def get_plan() -> Plan | None:
    plan = st.session_state.get(PLAN)
    if plan is None:
        return None
    if plan.expired or plan.consumed:
        clear_plan()
        return None
    return plan


def plan_matches(signature: str) -> bool:
    """A preview is only valid for the exact inputs it was built from."""
    return st.session_state.get(PLAN_SIGNATURE) == signature


def invalidate_plan_if_changed(signature: str):
    """Drop the preview as soon as a material field is edited."""
    if PLAN in st.session_state and st.session_state.get(PLAN_SIGNATURE) != signature:
        clear_plan()


def clear_plan():
    st.session_state.pop(PLAN, None)
    st.session_state.pop(PLAN_SIGNATURE, None)


# -- notices -------------------------------------------------------------
def set_notice(payload: dict):
    """A result that must survive the rerun that follows an action.

    Deliberately not a toast: an important outcome has to stay on screen until
    the user has read it.
    """
    st.session_state[NOTICE] = payload


def pop_notice() -> dict | None:
    return st.session_state.pop(NOTICE, None)


# -- double-submit guard --------------------------------------------------
def begin(operation_id: str) -> bool:
    """Claim the right to run one operation. False if it is already running."""
    if st.session_state.get(BUSY) == operation_id:
        return False
    st.session_state[BUSY] = operation_id
    return True


def finish():
    st.session_state.pop(BUSY, None)


def busy() -> bool:
    return BUSY in st.session_state


# -- caches --------------------------------------------------------------
def cache_get(key: str):
    return st.session_state.get(f"ucg_data_{key}")


def cache_put(key: str, value):
    st.session_state[f"ucg_data_{key}"] = value
    return value


def clear_caches():
    for key in [k for k in st.session_state if str(k).startswith("ucg_data_")]:
        del st.session_state[key]
    st.session_state.pop("ucg_running_as", None)
    clear_plan()
