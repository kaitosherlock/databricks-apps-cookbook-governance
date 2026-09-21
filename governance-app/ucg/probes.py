"""Finding out what this deployment can actually do.

The capability registry describes what the app *implements*; these probes
establish what the executing identity is *allowed* to do here. Each probe is a
single read-only call with the smallest possible page, and each records what
the failure proved: a refusal marks NO_PERMISSION, an absent API marks
UNSUPPORTED. The two are never conflated, because they need different people to
fix them.

Warehouse-backed probes are kept separate and never run automatically: starting
a SQL warehouse costs money, and nobody should be billed by opening a
diagnostics page.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .capabilities import CapabilityReport, State
from .naming import Target
from .services.base import Context


@dataclass
class ProbeResult:
    ran: int = 0
    usable: int = 0
    skipped: list[str] = None

    def __post_init__(self):
        if self.skipped is None:
            self.skipped = []


def _first_catalog(ctx: Context) -> str:
    """A catalog to probe object-scoped APIs against, or ''."""
    try:
        for catalog in ctx.w.catalogs.list(max_results=0, include_browse=True):
            name = getattr(catalog, "name", "") or ""
            if name and ctx.settings.in_scope(name):
                return name
    except Exception:
        return ""
    return ""


def _drain_one(iterator) -> None:
    """Force a lazy SDK iterator to make its first request."""
    for _ in iterator:
        break


def cheap_probes(ctx: Context) -> dict[str, Callable[[], object]]:
    """Read-only probes that cost nothing but a control-plane round trip."""
    w = ctx.w
    catalog = _first_catalog(ctx)
    probes: dict[str, Callable[[], object]] = {
        "assets.browse": lambda: _drain_one(w.catalogs.list(max_results=0)),
        "storage.credentials": lambda: _drain_one(
            w.storage_credentials.list(max_results=0, include_unbound=True)),
        "storage.service_credentials": lambda: _drain_one(
            w.credentials.list_credentials(max_results=0, include_unbound=True)),
        "storage.locations": lambda: _drain_one(
            w.external_locations.list(max_results=0, include_unbound=True)),
        "federation.connections": lambda: _drain_one(w.connections.list(max_results=0)),
        "sharing.shares": lambda: _drain_one(w.shares.list_shares(max_results=0)),
        "sharing.recipients": lambda: _drain_one(w.recipients.list(max_results=0)),
        "sharing.providers": lambda: _drain_one(w.providers.list(max_results=0)),
        "tags.policies": lambda: _drain_one(w.tag_policies.list_tag_policies(page_size=1)),
        "principals.lookup": lambda: _drain_one(w.groups.list(count=1)),
    }

    if catalog:
        target = Target("catalog", catalog)
        probes.update({
            "assets.metadata": lambda: w.catalogs.get(name=catalog, include_browse=True),
            "grants.read": lambda: w.grants.get(
                securable_type="CATALOG", full_name=catalog, max_results=0),
            "grants.effective": lambda: w.grants.get_effective(
                securable_type="CATALOG", full_name=catalog, max_results=0),
            "tags.read": lambda: _drain_one(
                w.entity_tag_assignments.list(entity_type="catalogs", entity_name=catalog)),
            "policies.read": lambda: _drain_one(w.policies.list_policies(
                on_securable_type="CATALOG", on_securable_fullname=catalog, max_results=0)),
            "storage.bindings": lambda: _drain_one(w.workspace_bindings.get_bindings(
                securable_type="catalog", securable_name=catalog, max_results=0)),
            "rfa.destinations": lambda: w.rfa.get_access_request_destinations(
                securable_type="catalog", full_name=catalog),
            "classification.config": lambda: w.data_classification.get_catalog_config(
                name=f"catalogs/{catalog}/config"),
        })
        del target
    return probes


def sql_probes(ctx: Context) -> dict[str, Callable[[], object]]:
    """Probes that need a SQL warehouse, and therefore cost money to run."""
    from .services.lineage import LineageService

    def lineage() -> object:
        availability = LineageService(ctx).available()
        if not availability.system_usable:
            raise _Unavailable(availability.detail)
        return availability

    def audit() -> object:
        from .services.audit_query import AuditService

        availability = AuditService(ctx).available()
        if not availability.usable:
            raise _Unavailable(availability.message)
        return availability

    return {"lineage.system": lineage, "audit.system": audit}


class _Unavailable(Exception):
    """Carries a probe's own explanation into the registry."""

    def __init__(self, detail: str = ""):
        super().__init__(detail or "")
        self.error_code = "PERMISSION_DENIED"


def run(report: CapabilityReport, probes: dict[str, Callable[[], object]]) -> ProbeResult:
    result = ProbeResult()
    for key, call in probes.items():
        cap = report.items.get(key)
        if cap is None:
            continue
        if cap.state in (State.NOT_CONFIGURED, State.UNSUPPORTED, State.NOT_IMPLEMENTED):
            result.skipped.append(key)
            continue
        result.ran += 1
        if report.probe(key, call):
            result.usable += 1
    # Capabilities that share a backing API with one that was probed inherit
    # its verdict, so the matrix does not leave obvious gaps unexplained.
    _mirror(report, "assets.browse", ["assets.edit", "assets.owner"])
    _mirror(report, "grants.read", ["grants.write"])
    _mirror(report, "tags.read", ["tags.write"])
    _mirror(report, "policies.read", ["policies.write", "masks.legacy"])
    return result


def _mirror(report: CapabilityReport, source_key: str, targets: list[str]):
    source = report.items.get(source_key)
    if source is None or source.state == State.UNKNOWN:
        return
    for key in targets:
        cap = report.items.get(key)
        if cap is None or cap.state != State.UNKNOWN:
            continue
        if source.state == State.NO_PERMISSION:
            cap.state = State.NO_PERMISSION
            cap.detail = (
                "Suy ra từ kết quả kiểm tra của cùng nhóm API: danh tính thực thi "
                "chưa đủ quyền đọc, nên cũng chưa thể ghi."
            )
        elif source.state in (State.AVAILABLE, State.READ_ONLY):
            cap.state = source.state
            cap.detail = cap.detail or (
                "Suy ra từ kết quả kiểm tra đọc của cùng nhóm API. "
                "Quyền ghi thực tế vẫn do Unity Catalog quyết định khi thực hiện."
            )
