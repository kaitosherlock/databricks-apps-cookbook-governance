"""The persistent frame: who you are, where you are, and what you may do.

In the Streamlit build this data was assembled by ``ui/chrome.py`` and drawn by
whichever page remembered to call it - which is why the PROD warning only ever
appeared on the search screen. Here it is a single endpoint that the shell reads
once and every screen renders from, so the environment and write-mode markers
cannot be present on one page and missing on another.
"""
from __future__ import annotations

from fastapi import APIRouter

from ucg.authz import Action
from ucg.config import Role
from ucg.identity import ExecutionIdentity
from ucg.services.base import Context

from .. import context as ctx_module
from .. import runtime
from ..http import CTX

router = APIRouter(prefix="/session", tags=["session"])

#: Environments that get an unmistakable marker. Never inferred from the host.
HIGH_RISK = {"PROD", "PRODUCTION"}


def _environment(ctx: Context) -> dict:
    env = ctx.settings.environment
    if not env:
        return {"name": "", "label": "Môi trường: chưa khai báo",
                "tone": "unknown", "high_risk": False}
    if env in HIGH_RISK:
        return {
            "name": env,
            "label": f"MÔI TRƯỜNG {env}",
            "tone": "danger",
            "high_risk": True,
            "warning": "Mọi thay đổi ở đây ảnh hưởng tới hệ thống thật.",
        }
    return {"name": env, "label": f"Môi trường {env}", "tone": "info", "high_risk": False}


def _mode(ctx: Context) -> dict:
    if not ctx.settings.writes_possible:
        return {
            "writable": False,
            "label": "Chỉ đọc",
            "reason": ctx.settings.write_block_reason() or "Ứng dụng đang ở chế độ chỉ đọc.",
        }
    if ctx.authz.allows(Action.GRANT):
        return {
            "writable": True,
            "label": "Được phép chỉnh sửa",
            "reason": "Bạn có thể cấp và thu hồi quyền trong phạm vi được cấu hình.",
        }
    return {
        "writable": False,
        "label": "Chỉ đọc",
        "reason": f"Vai trò “{ctx.authz.role_label}” không bao gồm quyền thay đổi.",
    }


def _host_label(ctx: Context) -> str:
    host = (ctx.connection.host or "").replace("https://", "").rstrip("/")
    return host or "Không xác định"


@router.get("")
def read_session(ctx: Context = CTX) -> dict:
    """Everything the app shell needs, on every page."""
    unresolved = ctx.log.needing_reconcile()
    pending = runtime.pending_plans(ctx.actor.email)

    return {
        "actor": {
            "email": ctx.actor.email,
            "label": ctx.actor.label,
            "verified": ctx.actor.verified,
            "trust_note": ctx.actor.trust_note,
            "role": ctx.authz.role,
            "role_label": ctx.authz.role_label,
            "role_description": Role.DESCRIPTIONS.get(ctx.authz.role, ""),
        },
        "execution": {
            "identity": ctx.execution_identity,
            "label": ExecutionIdentity.LABELS.get(
                ctx.execution_identity, ctx.execution_identity
            ),
            # Resolved lazily: it is one API call and the shell is read often.
            "running_as": _running_as(ctx),
            "explains_visibility": ctx.execution_identity
            == ExecutionIdentity.APP_SERVICE_PRINCIPAL,
        },
        "workspace": {"host": _host_label(ctx)},
        "environment": _environment(ctx),
        "mode": _mode(ctx),
        "scope": {
            "catalogs": sorted(ctx.settings.catalogs),
            "label": ctx.settings.scope_label,
            "unbounded_writes": not ctx.settings.catalogs and ctx.settings.writes_possible,
        },
        "permissions": ctx.authz.allowed_actions(),
        "pending_plans": [
            {"plan_id": p.plan_id, "target": p.target_name, "summary": p.summary}
            for p in pending
        ],
        "unresolved_operations": len(unresolved),
    }


_RUNNING_AS: dict[str, str] = {}


def _running_as(ctx: Context) -> str:
    """Who the API calls actually run as. Cached per identity, not globally."""
    key = ctx_module.identity_key(ctx)
    if key not in _RUNNING_AS:
        from ucg.clients import whoami

        try:
            _RUNNING_AS[key] = whoami(ctx.w) or ""
        except Exception:
            _RUNNING_AS[key] = ""
    return _RUNNING_AS[key]


@router.post("/refresh")
def refresh(ctx: Context = CTX) -> dict:
    """Drop cached reads and every pending preview, as the old sidebar button did."""
    runtime.reset_capabilities(ctx_module.identity_key(ctx))
    runtime.drop_plans_for(ctx.actor.email)
    _RUNNING_AS.pop(ctx_module.identity_key(ctx), None)
    return {"ok": True}
