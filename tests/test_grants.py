"""Grant reading and the full mutation pipeline."""
from __future__ import annotations

import pytest

from conftest import FakeDatabricksError, make_context
from ucg.audit import Status
from ucg.errors import Code, GovernanceError
from ucg.naming import Target
from ucg.services.grants import DIRECT, INHERITED, OWNERSHIP, GrantService

TABLE = Target("table", "catalog1", "schema1", "routes")
CATALOG = Target("catalog", "catalog1")


def service(settings, client, email="admin@x.com") -> GrantService:
    return GrantService(make_context(settings, client, email))


# -- reading -------------------------------------------------------------
def test_direct_grants_exclude_inherited(settings, table_client):
    view = service(settings, table_client).direct(TABLE)
    assert view.ok
    assert {(r.principal, r.privilege) for r in view.rows} == {("analysts", "SELECT")}
    assert all(r.source == DIRECT for r in view.rows)


def test_effective_grants_carry_provenance(settings, table_client):
    view = service(settings, table_client).effective(TABLE)
    assert view.ok
    by_key = {(r.principal, r.privilege): r for r in view.rows}

    direct = by_key[("analysts", "SELECT")]
    assert direct.source == DIRECT
    assert direct.revocable_here is True

    inherited = by_key[("analysts", "USE_CATALOG")]
    assert inherited.source == INHERITED
    assert inherited.inherited_from == "catalog1"
    assert inherited.source_label == "Kế thừa từ catalog"
    assert inherited.revocable_here is False

    from_schema = by_key[("engineers", "MODIFY")]
    assert from_schema.source_label == "Kế thừa từ schema"


def test_direct_and_inherited_are_never_confused(settings, table_client):
    """The same privilege from two sources stays two distinguishable rows."""
    table_client.grants.direct["catalog1.schema1.routes"]["engineers"] = {"MODIFY"}
    view = service(settings, table_client).effective(TABLE)
    modify = [r for r in view.rows if r.principal == "engineers" and r.privilege == "MODIFY"]
    assert {r.source for r in modify} == {DIRECT, INHERITED}
    assert sum(1 for r in modify if r.revocable_here) == 1


def test_pagination_survives_an_empty_page_with_a_token(settings, table_client):
    """An empty page carrying a next token must not truncate the listing."""
    view = service(settings, table_client).direct(TABLE)
    tokens = [c[4] for c in table_client.grants.calls if c[0] == "get"]
    assert tokens == [None, "page2"], "the second, empty page must still be read"
    assert view.rows


def test_permissions_page_size_never_lands_in_the_forbidden_band(settings, table_client):
    service(settings, table_client).direct(TABLE)
    sizes = {c[3] for c in table_client.grants.calls if c[0] == "get"}
    for size in sizes:
        assert size == 0 or size >= 150, f"max_results={size} is rejected by Databricks"


def test_owner_is_added_separately_and_marked(settings, table_client):
    svc = service(settings, table_client)
    view = svc.with_owner(svc.direct(TABLE), "owner@x.com")
    owner_rows = [r for r in view.rows if r.source == OWNERSHIP]
    assert len(owner_rows) == 1
    assert owner_rows[0].principal == "owner@x.com"
    assert not owner_rows[0].revocable_here


def test_permission_denied_is_reported_not_rendered_as_empty(settings, table_client):
    table_client.grants.fail_with = FakeDatabricksError("PERMISSION_DENIED")
    view = service(settings, table_client).direct(TABLE)
    assert not view.ok
    assert view.error.code == Code.PERMISSION_DENIED
    assert view.rows == []


def test_upstream_message_never_leaks(settings, table_client):
    table_client.grants.fail_with = FakeDatabricksError(
        "PERMISSION_DENIED", "token=abcd1234 secret leak"
    )
    view = service(settings, table_client).direct(TABLE)
    assert "abcd1234" not in view.error.message
    assert "abcd1234" not in (view.error.detail or "")


# -- privilege offering ---------------------------------------------------
def test_view_is_not_offered_modify(settings, table_client):
    svc = service(settings, table_client)
    codes = svc.available_privileges(TABLE, table_type="VIEW")
    assert "MODIFY" not in codes
    assert "SELECT" in codes


def test_browse_is_catalog_level_only(settings, table_client):
    svc = service(settings, table_client)
    assert "BROWSE" not in svc.available_privileges(TABLE)
    assert "BROWSE" in svc.available_privileges(CATALOG)


def test_only_sdk_known_privileges_are_offered(settings, table_client):
    from databricks.sdk.service.catalog import Privilege

    known = {p.value for p in Privilege}
    svc = service(settings, table_client)
    for target in (TABLE, CATALOG):
        for code in svc.available_privileges(target):
            assert code in known, f"{code} would fail at apply time"


# -- planning -------------------------------------------------------------
def test_plan_requires_a_reason(settings, table_client):
    svc = service(settings, table_client)
    with pytest.raises(GovernanceError) as err:
        svc.plan_change(TABLE, "analysts", "grant", ["MODIFY"], "")
    assert err.value.code == Code.INVALID_INPUT


def test_plan_rejects_a_privilege_the_object_cannot_take(settings, table_client):
    svc = service(settings, table_client)
    with pytest.raises(GovernanceError) as err:
        svc.plan_change(TABLE, "analysts", "grant", ["CREATE_SCHEMA"], "vì lý do")
    assert err.value.code == Code.INVALID_PRIVILEGE


def test_revoking_an_inherited_privilege_is_refused(settings, table_client):
    """engineers hold MODIFY only through the schema, so it is not revocable here.

    MODIFY is perfectly valid on a table, which is what makes this the real
    case: the privilege passes type validation and is still refused, with the
    reason pointing at the parent.
    """
    svc = service(settings, table_client)
    with pytest.raises(GovernanceError) as err:
        svc.plan_change(TABLE, "engineers", "revoke", ["MODIFY"], "dọn quyền")
    assert "kế thừa" in err.value.message.lower()
    assert "cấp cha" in err.value.message.lower()


def test_revoking_a_privilege_the_object_cannot_hold_is_refused_by_type(settings, table_client):
    svc = service(settings, table_client)
    with pytest.raises(GovernanceError) as err:
        svc.plan_change(TABLE, "analysts", "revoke", ["USE_CATALOG"], "dọn quyền")
    assert err.value.code == Code.INVALID_PRIVILEGE


def test_granting_what_is_already_held_is_refused(settings, table_client):
    svc = service(settings, table_client)
    with pytest.raises(GovernanceError) as err:
        svc.plan_change(TABLE, "analysts", "grant", ["SELECT"], "vì lý do")
    assert err.value.code == Code.ALREADY_SATISFIED


def test_preview_says_child_impact_is_undetermined_for_a_catalog(settings, table_client):
    svc = service(settings, table_client)
    plan = svc.plan_change(CATALOG, "analysts", "grant", ["SELECT"], "cấp cho nhóm phân tích")
    text = " ".join(line.text for line in plan.preview)
    assert "Chưa xác định" in text, "must not invent an affected-object count"
    assert "kế thừa" in text.lower()


def test_preview_names_the_execution_identity(settings, table_client):
    svc = service(settings, table_client)
    plan = svc.plan_change(TABLE, "newcomer", "grant", ["SELECT"], "lý do")
    text = " ".join(line.text for line in plan.preview)
    assert "tài khoản dịch vụ" in text.lower()


def test_revoke_preview_warns_access_may_remain(settings, table_client):
    svc = service(settings, table_client)
    plan = svc.plan_change(TABLE, "analysts", "revoke", ["SELECT"], "thu hồi")
    text = " ".join(line.text for line in plan.preview)
    assert "không đảm bảo" in text.lower()


# -- applying -------------------------------------------------------------
def test_apply_sends_only_a_delta_for_one_principal(settings, table_client):
    svc = service(settings, table_client)
    plan = svc.plan_change(TABLE, "newcomer", "grant", ["SELECT"], "lý do")
    outcome = svc.apply(plan, TABLE, TABLE.full_name)

    assert outcome.status == Status.VERIFIED
    update = [c for c in table_client.grants.calls if c[0] == "update"][0]
    changes = update[3]
    assert len(changes) == 1
    assert changes[0].principal == "newcomer"
    assert [p.value for p in changes[0].add] == ["SELECT"]
    assert changes[0].remove is None
    # The pre-existing grant for another principal is untouched.
    assert table_client.grants.direct["catalog1.schema1.routes"]["analysts"] == {"SELECT"}


def test_apply_verifies_by_reading_back(settings, table_client):
    svc = service(settings, table_client)
    plan = svc.plan_change(TABLE, "analysts", "revoke", ["SELECT"], "thu hồi")
    outcome = svc.apply(plan, TABLE, TABLE.full_name)
    assert outcome.status == Status.VERIFIED
    assert outcome.verified_state["applied"] is True
    assert outcome.verified_state["privileges_now"] == []


def test_confirmation_must_match_the_object_name(settings, table_client):
    svc = service(settings, table_client)
    plan = svc.plan_change(TABLE, "newcomer", "grant", ["SELECT"], "lý do")
    with pytest.raises(GovernanceError) as err:
        svc.apply(plan, TABLE, "wrong.name")
    assert err.value.code == Code.INVALID_INPUT
    assert not any(c[0] == "update" for c in table_client.grants.calls)


def test_a_plan_cannot_be_applied_twice(settings, table_client):
    svc = service(settings, table_client)
    plan = svc.plan_change(TABLE, "newcomer", "grant", ["SELECT"], "lý do")
    svc.apply(plan, TABLE, TABLE.full_name)
    with pytest.raises(GovernanceError) as err:
        svc.apply(plan, TABLE, TABLE.full_name)
    assert err.value.code == Code.STALE_PLAN
    assert sum(1 for c in table_client.grants.calls if c[0] == "update") == 1


def test_concurrent_change_between_preview_and_apply_aborts(settings, table_client):
    svc = service(settings, table_client)
    plan = svc.plan_change(TABLE, "newcomer", "grant", ["SELECT"], "lý do")

    def someone_else_grants_it():
        table_client.grants.direct["catalog1.schema1.routes"]["newcomer"] = {"SELECT"}

    table_client.grants.on_get = someone_else_grants_it
    with pytest.raises(GovernanceError) as err:
        svc.apply(plan, TABLE, TABLE.full_name)
    assert err.value.code in (Code.STALE_PLAN, Code.ALREADY_SATISFIED)
    assert not any(c[0] == "update" for c in table_client.grants.calls)


def test_timeout_during_apply_reports_unknown_not_failure(settings, table_client):
    svc = service(settings, table_client)
    plan = svc.plan_change(TABLE, "newcomer", "grant", ["SELECT"], "lý do")

    class Timeout(Exception):
        pass
    Timeout.__name__ = "DeadlineExceeded"

    original = table_client.grants.update

    def exploding(*a, **k):
        raise Timeout("no response")

    table_client.grants.update = exploding
    outcome = svc.apply(plan, TABLE, TABLE.full_name)
    table_client.grants.update = original

    assert outcome.status == Status.UNKNOWN
    assert outcome.unknown
    assert outcome.error.outcome_unknown
    assert "chưa xác định" in outcome.error.next_step.lower()


def test_viewer_cannot_apply_even_with_a_valid_plan(settings, table_client):
    admin = service(settings, table_client, "admin@x.com")
    plan = admin.plan_change(TABLE, "newcomer", "grant", ["SELECT"], "lý do")

    viewer = service(settings, table_client, "viewer@x.com")
    with pytest.raises(GovernanceError) as err:
        viewer.apply(plan, TABLE, TABLE.full_name)
    assert err.value.code == Code.FORBIDDEN_ROLE
    assert not any(c[0] == "update" for c in table_client.grants.calls)


def test_a_plan_from_another_actor_is_rejected(settings, table_client):
    admin = service(settings, table_client, "admin@x.com")
    plan = admin.plan_change(TABLE, "newcomer", "grant", ["SELECT"], "lý do")

    other = service(settings, table_client, "platform@x.com")
    with pytest.raises(GovernanceError) as err:
        other.apply(plan, TABLE, TABLE.full_name)
    assert err.value.code == Code.STALE_PLAN


def test_operation_is_logged_with_both_identities(settings, table_client):
    ctx = make_context(settings, table_client, "admin@x.com")
    svc = GrantService(ctx)
    plan = svc.plan_change(TABLE, "newcomer", "grant", ["SELECT"], "vì dự án X")
    svc.apply(plan, TABLE, TABLE.full_name)

    event = ctx.log.events[0]
    assert event.actor == "admin@x.com"
    assert event.execution_identity == "app_service_principal"
    assert event.reason == "vì dự án X"
    assert event.status == Status.VERIFIED
    assert event.principal == "newcomer"


# -- privileges the pinned SDK cannot name --------------------------------
def test_a_privilege_the_sdk_cannot_name_is_kept_and_labelled(settings, table_client):
    """Databricks names privileges the pinned enum lacks (e.g. READ METADATA).

    The SDK parses those to None. Dropping the row would under-report access,
    so it is kept, labelled, and excluded from anything that writes.
    """
    from conftest import Box, Page

    def effective_with_a_blank(securable_type, full_name, **kwargs):
        if kwargs.get("page_token") is not None:
            return Page([], None)
        return Page([Box(principal="account users", privileges=[
            Box(privilege="MANAGE", inherited_from_name="catalog1",
                inherited_from_type="CATALOG"),
            Box(privilege=None, inherited_from_name="catalog1",
                inherited_from_type="CATALOG"),
        ])], None)

    table_client.grants.get_effective = effective_with_a_blank
    view = service(settings, table_client).effective(TABLE)

    assert view.ok
    assert view.has_unreadable
    blank = [r for r in view.rows if r.unreadable]
    assert len(blank) == 1
    rendered = blank[0].row()
    assert rendered["Quyền"] == "Không đọc được mã quyền"
    assert rendered["Nguồn quyền"] == "Kế thừa từ catalog"
    # It must never be offered as something to revoke.
    assert blank[0].revocable_here is False


def test_an_unreadable_privilege_never_reaches_a_revoke_payload(settings, table_client):
    from conftest import Box, Page

    def direct_with_a_blank(securable_type, full_name, **kwargs):
        if kwargs.get("page_token") is not None:
            return Page([], None)
        return Page([Box(principal="analysts", privileges=["SELECT", None])], None)

    table_client.grants.get = direct_with_a_blank
    view = service(settings, table_client).direct(TABLE)
    assert view.direct_privileges("analysts") == ["SELECT"]


def test_principal_type_is_derived_the_way_unity_catalog_reads_it(settings, table_client):
    from ucg.services.grants import classify_principal

    assert classify_principal("edison696996@gmail.com") == "Người dùng"
    assert classify_principal("bb651242-8fd8-4274-a914-ea3d020c5373") == "Service principal"
    assert classify_principal("account users") == "Nhóm"
    assert classify_principal("") == "Chưa xác định"
