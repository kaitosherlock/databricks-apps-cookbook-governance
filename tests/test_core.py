"""Naming, privileges, pagination, errors and the plan lifecycle."""
from __future__ import annotations

import pytest

from conftest import FakeDatabricksError
from ucg import privileges as priv
from ucg.errors import Code, GovernanceError, translate
from ucg.naming import Target, sql_identifier, validate_component
from ucg.paging import Completeness, collect_pages, permissions_page_size
from ucg.plans import Plan, fingerprint


# -- naming and SQL safety ----------------------------------------------
@pytest.mark.parametrize("bad", [
    "a`b",            # backtick would break identifier quoting
    "a.b",            # dot is the component separator
    "a\x00b",         # NUL
    "a\nb",           # control character
    "",               # empty
    "x" * 256,        # too long
])
def test_invalid_name_components_are_rejected(bad):
    with pytest.raises(GovernanceError) as err:
        validate_component(bad)
    assert err.value.code == Code.INVALID_NAME


def test_sql_identifier_quotes_each_component():
    assert sql_identifier("cat", "sch", "tbl") == "`cat`.`sch`.`tbl`"


def test_sql_identifier_refuses_an_injection_attempt():
    with pytest.raises(GovernanceError):
        sql_identifier("cat`; DROP TABLE x; --")


def test_target_requires_the_right_number_of_components():
    with pytest.raises(GovernanceError):
        Target("table", "cat", "sch")  # missing the object name
    with pytest.raises(GovernanceError):
        Target("catalog", "cat", "sch")  # catalogs have no child components


def test_target_parse_round_trip():
    target = Target.parse("table", "cat.sch.tbl")
    assert (target.catalog, target.schema, target.name) == ("cat", "sch", "tbl")
    assert target.full_name == "cat.sch.tbl"
    assert target.securable_type == "TABLE"


def test_registered_model_is_addressed_as_a_function_securable():
    """Unity Catalog has no model securable type - models are FUNCTIONs."""
    assert Target("model", "c", "s", "m").securable_type == "FUNCTION"


def test_parent_chain():
    table = Target("table", "c", "s", "t")
    assert table.parent == Target("schema", "c", "s")
    assert [a.full_name for a in table.ancestors()] == ["c.s", "c"]


# -- privileges ----------------------------------------------------------
def test_recipients_and_providers_accept_no_privileges():
    assert "RECIPIENT" in priv.NOT_GRANTABLE
    assert "PROVIDER" in priv.NOT_GRANTABLE


def test_all_privileges_explanation_excludes_manage():
    text = priv.info("ALL_PRIVILEGES").explanation
    assert "MANAGE" in text
    assert "KHÔNG" in text


def test_privilege_display_shows_label_and_code():
    assert priv.display("SELECT") == "Đọc dữ liệu — SELECT"


def test_materialized_view_gains_refresh_but_loses_modify():
    codes = priv.for_securable("TABLE", table_type="MATERIALIZED_VIEW")
    assert "REFRESH" in codes
    assert "MODIFY" not in codes


def test_plain_table_is_not_offered_refresh():
    assert "REFRESH" not in priv.for_securable("TABLE", table_type="MANAGED")


def test_model_privileges_exclude_select():
    codes = priv.for_securable("FUNCTION", kind="model")
    assert "SELECT" not in codes
    assert "EXECUTE" in codes


def test_manage_warning_mentions_self_grant():
    warnings = priv.warnings_for(["MANAGE"], "table")
    assert any("tự cấp" in w for w in warnings)


def test_catalog_grant_warns_about_inheritance():
    warnings = priv.warnings_for(["SELECT"], "catalog")
    assert any("kế thừa" in w.lower() for w in warnings)


def test_traversal_note_is_present_for_child_objects():
    assert "USE_CATALOG" in priv.traversal_note("table")
    assert priv.traversal_note("catalog") == ""


# -- pagination ----------------------------------------------------------
def test_page_size_avoids_the_forbidden_band():
    assert permissions_page_size(None) == 0
    assert permissions_page_size(0) == 0
    assert permissions_page_size(1) == 150
    assert permissions_page_size(149) == 150
    assert permissions_page_size(300) == 300


def test_collect_pages_reads_past_an_empty_page():
    pages = [([], "t1"), (["a"], "t2"), ([], None)]
    calls = []

    def fetch(token):
        calls.append(token)
        return pages[len(calls) - 1]

    items, completeness = collect_pages(fetch)
    assert items == ["a"]
    assert completeness == Completeness.COMPLETE
    assert len(calls) == 3


def test_collect_pages_flags_truncation_instead_of_lying():
    counter = {"n": 0}

    def fetch(token):
        counter["n"] += 1
        # A genuinely long listing hands back a fresh token each page.
        return (["x"], f"page-{counter['n']}")

    items, completeness = collect_pages(fetch, max_pages=3)
    assert completeness == Completeness.TRUNCATED
    assert len(items) == 3


def test_repeated_page_token_is_an_error_not_an_infinite_loop():
    def fetch(token):
        return (["x"], "same")

    # The guard only trips when the same token comes back twice.
    with pytest.raises(GovernanceError):
        collect_pages(lambda t: (["x"], "same" if t != "same" else "same"), max_pages=10)


# -- errors --------------------------------------------------------------
def test_permission_denied_is_mapped_and_explained():
    err = translate(FakeDatabricksError("PERMISSION_DENIED"))
    assert err.code == Code.PERMISSION_DENIED
    assert err.next_step


def test_unknown_upstream_code_does_not_leak_the_body():
    err = translate(FakeDatabricksError("WEIRD_CODE", "bearer eyJhbGciOi... secret"))
    assert "eyJhbGciOi" not in err.message
    assert err.code == Code.UPSTREAM_ERROR


def test_timeout_on_a_mutation_is_unknown_not_failed():
    class DeadlineExceeded(Exception):
        pass

    err = translate(DeadlineExceeded("no answer"), mutating=True)
    assert err.code == Code.TIMEOUT_UNKNOWN
    assert err.outcome_unknown
    assert "trước khi thử lại" in err.next_step.lower()


def test_timeout_on_a_read_is_just_an_upstream_error():
    class DeadlineExceeded(Exception):
        pass

    err = translate(DeadlineExceeded("no answer"), mutating=False)
    assert err.code == Code.UPSTREAM_ERROR
    assert not err.outcome_unknown


def test_every_error_carries_a_correlation_id():
    err = translate(ValueError("boom"))
    assert err.correlation_id
    assert err.code == Code.INTERNAL
    assert "boom" not in err.message


def test_feature_disabled_is_a_capability_problem_not_a_permission_one():
    err = translate(FakeDatabricksError("FEATURE_DISABLED"))
    assert err.code == Code.CAPABILITY_UNAVAILABLE


# -- plans ---------------------------------------------------------------
def _plan(**overrides) -> Plan:
    base = dict(
        action="grant", target_key="table:c.s.t", target_name="c.s.t",
        target_type="Bảng", actor="a@x.com", execution_identity="app_service_principal",
        payload={"principal": "p"}, before=fingerprint(["SELECT"]), reason="lý do",
        summary="Cấp quyền SELECT",
    )
    base.update(overrides)
    return Plan(**base)


def test_plan_bound_to_actor_target_and_identity():
    plan = _plan()
    plan.require_fresh("a@x.com", "app_service_principal", "table:c.s.t")
    with pytest.raises(GovernanceError):
        plan.require_fresh("b@x.com", "app_service_principal", "table:c.s.t")
    with pytest.raises(GovernanceError):
        plan.require_fresh("a@x.com", "end_user", "table:c.s.t")
    with pytest.raises(GovernanceError):
        plan.require_fresh("a@x.com", "app_service_principal", "table:other")


def test_consumed_plan_cannot_be_reused():
    plan = _plan()
    plan.consumed = True
    with pytest.raises(GovernanceError) as err:
        plan.require_fresh("a@x.com", "app_service_principal", "table:c.s.t")
    assert err.value.code == Code.STALE_PLAN


def test_expired_plan_is_refused():
    from datetime import datetime, timedelta, timezone
    plan = _plan()
    plan.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    assert plan.expired
    with pytest.raises(GovernanceError):
        plan.require_fresh("a@x.com", "app_service_principal", "table:c.s.t")


def test_state_change_since_preview_is_refused():
    plan = _plan()
    plan.require_unchanged(fingerprint(["SELECT"]))
    with pytest.raises(GovernanceError) as err:
        plan.require_unchanged(fingerprint(["SELECT", "MODIFY"]))
    assert err.value.code == Code.STALE_PLAN


def test_confirmation_must_match_exactly():
    plan = _plan()
    plan.require_confirmation("c.s.t")
    with pytest.raises(GovernanceError):
        plan.require_confirmation("c.s.")


def test_partial_outcome_is_not_reported_as_success():
    from ucg.audit import Status
    from ucg.plans import ObjectOutcome, Outcome

    outcome = Outcome(status=Status.APPLIED, per_object=[
        ObjectOutcome("a", Status.APPLIED),
        ObjectOutcome("b", Status.FAILED, "không đủ quyền"),
    ])
    assert outcome.partial
    assert "một phần" in outcome.headline().lower()
