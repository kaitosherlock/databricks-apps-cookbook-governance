"""Shared plumbing for every governance service.

Services are UI-independent: they take a :class:`Context` and return plain
dicts/dataclasses. Nothing in this package imports Streamlit, which is what
makes the backend testable without a browser and reusable behind a different
front end later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..audit import Event, OperationLog, Status
from ..authz import Authorizer
from ..capabilities import CapabilityReport
from ..clients import Connection
from ..config import Settings
from ..errors import GovernanceError, translate
from ..identity import Actor
from ..naming import Target
from ..plans import Outcome, Plan


@dataclass
class Context:
    """Everything a service needs, assembled once per request."""

    settings: Settings
    actor: Actor
    connection: Connection
    authz: Authorizer
    capabilities: CapabilityReport
    log: OperationLog
    #: Optional second connection that acts as the signed-in user. Present only
    #: when the app declares user-authorization scopes. Never used as a silent
    #: substitute for the service principal, or the other way round.
    user_connection: Connection | None = None

    @property
    def w(self):
        return self.connection.client

    @property
    def execution_identity(self) -> str:
        return self.connection.execution_identity


def as_dict(obj: Any) -> dict:
    """SDK dataclass -> plain dict, tolerating already-plain values."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "as_dict"):
        try:
            return obj.as_dict()
        except Exception:
            pass
    return dict(getattr(obj, "__dict__", {}) or {})


def enum_value(value: Any) -> str:
    """Unwrap an SDK enum to its wire string."""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def millis_to_text(value) -> str:
    """Databricks timestamps are epoch milliseconds."""
    if not value:
        return ""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ""


@dataclass
class Listing:
    """A list plus the honesty fields the brief requires.

    ``completeness`` and ``observed_at`` travel with every listing so a screen
    can say "this is what we could see, as of then" instead of presenting a
    partial answer as the whole truth.
    """

    items: list = field(default_factory=list)
    completeness: str = "complete"
    observed_at: str = ""
    note: str = ""
    #: Set when the listing failed for a reason the user should see.
    error: GovernanceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def empty(self) -> bool:
        return not self.items

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)


def now_text() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Service:
    """Base class: holds the context and standardises error handling."""

    #: Capability keys this service needs. Used to explain a disabled screen.
    capability_keys: tuple[str, ...] = ()

    def __init__(self, ctx: Context):
        self.ctx = ctx

    @property
    def w(self):
        return self.ctx.w

    @property
    def settings(self) -> Settings:
        return self.ctx.settings

    @property
    def authz(self) -> Authorizer:
        return self.ctx.authz

    # -- safe calling -----------------------------------------------------
    def read(self, call: Callable[[], Any], *, default=None):
        """Run a read and convert any failure into a GovernanceError."""
        try:
            return call()
        except Exception as exc:
            raise translate(exc) from None

    def listing(self, call: Callable[[], Listing]) -> Listing:
        """Run a listing, returning the error inside the Listing.

        Screens show several listings at once; one failing panel should explain
        itself rather than blanking the page.
        """
        try:
            return call()
        except Exception as exc:
            return Listing(items=[], completeness="partial_permission",
                           observed_at=now_text(), error=translate(exc))

    # -- the mutation pipeline -------------------------------------------
    def execute(
        self,
        plan: Plan,
        target: Target | None,
        apply: Callable[[], Any],
        *,
        revalidate: Callable[[], str] | None = None,
        verify: Callable[[], dict] | None = None,
        action_label: str = "",
    ) -> Outcome:
        """Revalidate -> Execute -> Verify -> Audit, with honest outcomes.

        ``revalidate`` recomputes the state fingerprint; a mismatch aborts
        before anything is sent. ``verify`` re-reads state afterwards so the UI
        can show what Databricks actually holds now, rather than echoing back
        what was requested.
        """
        ctx = self.ctx
        # 1. Authorise again on the server, regardless of what the UI allowed.
        ctx.authz.require(plan.action, target)

        # 2. One plan, one submission.
        plan.require_fresh(ctx.actor.email, ctx.execution_identity, plan.target_key)

        # 3. Refuse to act on a stale view of the world.
        if revalidate is not None:
            plan.require_unchanged(revalidate())

        plan.consumed = True
        plan.status = Status.APPLYING
        event = ctx.log.record(Event(
            action=plan.action,
            target=plan.target_name,
            target_type=plan.target_type,
            actor=ctx.actor.email,
            execution_identity=ctx.execution_identity,
            status=Status.APPLYING,
            summary=action_label or plan.summary,
            reason=plan.reason,
            principal=str(plan.payload.get("principal", "")),
            details={k: v for k, v in plan.payload.items() if k != "principal"},
            operation_id=plan.operation_id,
        ))

        # 4. Execute.
        try:
            apply()
        except Exception as exc:
            err = translate(exc, mutating=True)
            status = Status.UNKNOWN if err.outcome_unknown else Status.FAILED
            plan.status = status
            ctx.log.update(event.event_id, status=status, error_code=err.code,
                           correlation_id=err.correlation_id)
            return Outcome(status=status, event_id=event.event_id,
                           operation_id=plan.operation_id, error=err,
                           message=err.message)

        plan.status = Status.APPLIED
        ctx.log.update(event.event_id, status=Status.APPLIED)

        # 5. Verify by reading back. A failure here does not undo anything, so
        #    it is reported as "applied but unverified", never as failure.
        verified: dict | None = None
        status = Status.APPLIED
        if verify is not None:
            try:
                verified = verify()
                status = Status.VERIFIED
            except Exception as exc:
                err = translate(exc)
                status = Status.VERIFY_FAILED
                ctx.log.update(event.event_id, status=status, error_code=err.code)
                return Outcome(status=status, event_id=event.event_id,
                               operation_id=plan.operation_id,
                               message="Databricks đã nhận thay đổi nhưng chưa đọc lại được trạng thái để xác minh.")
            ctx.log.update(event.event_id, status=status)

        plan.status = status
        return Outcome(status=status, event_id=event.event_id,
                       operation_id=plan.operation_id, verified_state=verified)
