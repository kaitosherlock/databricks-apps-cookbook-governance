"""Capability probing must distinguish refusal from absence."""
from __future__ import annotations

from conftest import FakeDatabricksError, FakeWorkspaceClient, make_context
from ucg import probes
from ucg.capabilities import CapabilityReport, State


def test_a_successful_probe_marks_the_capability_usable(settings, table_client):
    ctx = make_context(settings, table_client)
    report = CapabilityReport(settings)
    result = probes.run(report, probes.cheap_probes(ctx))

    assert result.ran > 0
    assert report.get("assets.browse").state in (State.AVAILABLE, State.READ_ONLY)
    assert report.get("grants.read").usable


def test_a_refusal_marks_no_permission_not_unsupported(settings, table_client):
    table_client.grants.fail_with = FakeDatabricksError("PERMISSION_DENIED")
    ctx = make_context(settings, table_client)
    report = CapabilityReport(settings)
    probes.run(report, probes.cheap_probes(ctx))

    cap = report.get("grants.read")
    assert cap.state == State.NO_PERMISSION, "a missing privilege is not a missing feature"
    assert "quyền" in cap.detail.lower()


def test_an_absent_api_marks_unsupported_not_no_permission(settings, table_client):
    table_client.grants.fail_with = FakeDatabricksError("ENDPOINT_NOT_FOUND")
    ctx = make_context(settings, table_client)
    report = CapabilityReport(settings)
    probes.run(report, probes.cheap_probes(ctx))

    cap = report.get("grants.read")
    assert cap.state == State.UNSUPPORTED
    assert "quyền" not in cap.detail.lower()


def test_a_read_refusal_is_mirrored_onto_the_matching_write(settings, table_client):
    table_client.grants.fail_with = FakeDatabricksError("PERMISSION_DENIED")
    ctx = make_context(settings, table_client)
    report = CapabilityReport(settings)
    probes.run(report, probes.cheap_probes(ctx))

    assert report.get("grants.write").state == State.NO_PERMISSION


def test_warehouse_backed_capabilities_are_not_probed_automatically(settings, table_client):
    """Starting a warehouse costs money; a diagnostics page must not do it."""
    ctx = make_context(settings, table_client)
    assert "lineage.system" not in probes.cheap_probes(ctx)
    assert "audit.system" not in probes.cheap_probes(ctx)
    assert set(probes.sql_probes(ctx)) == {"lineage.system", "audit.system"}


def test_capabilities_needing_an_unset_warehouse_report_not_configured():
    from ucg.config import Settings

    settings = Settings(enable_writes=True, warehouse_id="")
    report = CapabilityReport(settings)
    assert report.get("lineage.system").state == State.NOT_CONFIGURED
    assert "GOVERNANCE_WAREHOUSE_ID" in report.get("lineage.system").detail


def test_probing_never_mutates_the_shared_registry(settings, table_client):
    """Two sessions must not see each other's probe results."""
    ctx = make_context(settings, table_client)
    first = CapabilityReport(settings)
    probes.run(first, probes.cheap_probes(ctx))

    second = CapabilityReport(settings)
    assert second.get("assets.browse").state == State.UNKNOWN


def test_rfa_approval_is_always_reported_unsupported(settings):
    report = CapabilityReport(settings)
    cap = report.get("rfa.approve")
    assert cap.state == State.UNSUPPORTED
    assert "phê duyệt" in cap.detail.lower()
