from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from databricks.sdk.service.catalog import GetPermissionsResponse, EffectivePermissionsList
from governance.service import GovernanceService, GovernanceError, Settings, Target, grant_rows, actor_from_headers
from governance.demo import demo_client

TARGET = Target("table", "demo_governance", "curated", "customers")
SETTINGS = Settings(frozenset({"demo_governance"}), frozenset({"admin@example.com"}), writes=True)


def service(settings=SETTINGS, actor="admin@example.com"):
    w = demo_client()
    w.grants.update = Mock()
    return GovernanceService(w, settings, actor)


@pytest.mark.parametrize("settings,actor", [
    (replace(SETTINGS, writes=False), "admin@example.com"),
    (replace(SETTINGS, local=True), "admin@example.com"),
    (replace(SETTINGS, demo=True), "admin@example.com"),
    (SETTINGS, ""), (SETTINGS, "reader@example.com"),
    (replace(SETTINGS, catalogs=frozenset()), "admin@example.com"),
])
def test_mutations_fail_closed(settings, actor):
    s = service(settings, actor)
    with pytest.raises(GovernanceError):
        s.prepare(TARGET, "analysts", "grant", ["MODIFY"], "Approved ticket")
    s.w.grants.update.assert_not_called()


def test_delta_changes_only_selected_principal_and_privilege():
    s = service()
    change = s.prepare(TARGET, "analysts", "grant", ["MODIFY"], "Approved ticket")
    result = s.apply(change, TARGET.full_name)
    kwargs = s.w.grants.update.call_args.kwargs
    assert kwargs["securable_type"] == "table"
    assert kwargs["full_name"] == TARGET.full_name
    assert [x.as_dict() for x in kwargs["changes"]] == [{"principal": "analysts", "add": ["MODIFY"]}]
    assert result["status"] == "succeeded"


def test_revoke_is_direct_only():
    s = service()
    with pytest.raises(GovernanceError, match="trực tiếp"):
        s.prepare(TARGET, "reporting", "revoke", ["SELECT"], "Inherited only")
    change = s.prepare(TARGET, "analysts", "revoke", ["SELECT"], "Remove direct")
    s.apply(change, TARGET.full_name)
    assert s.w.grants.update.call_args.kwargs["changes"][0].as_dict() == {"principal": "analysts", "remove": ["SELECT"]}


def test_stale_preview_never_mutates():
    s = service()
    change = s.prepare(TARGET, "analysts", "grant", ["MODIFY"], "Approved ticket")
    s.w.grants.get = lambda **kw: GetPermissionsResponse(privilege_assignments=[])
    with pytest.raises(GovernanceError, match="đã thay đổi"):
        s.apply(change, TARGET.full_name)
    s.w.grants.update.assert_not_called()


def test_confirmation_and_policy_rechecked_at_apply():
    s = service()
    change = s.prepare(TARGET, "analysts", "grant", ["MODIFY"], "Approved ticket")
    with pytest.raises(GovernanceError):
        s.apply(change, "wrong")
    s.settings = replace(SETTINGS, writes=False)
    with pytest.raises(GovernanceError):
        s.apply(change, TARGET.full_name)
    s.w.grants.update.assert_not_called()


def test_pagination_continues_after_empty_page():
    s = service()
    s.w.grants.get = Mock(side_effect=[
        GetPermissionsResponse(privilege_assignments=[], next_page_token="second"),
        GetPermissionsResponse.from_dict({"privilege_assignments": [{"principal": "a", "privileges": ["SELECT"]}]}),
    ])
    assert s.grants(TARGET)[0]["principal"] == "a"
    assert s.w.grants.get.call_args.kwargs["page_token"] == "second"


def test_pagination_loop_fails_instead_of_returning_incomplete_results():
    s = service()
    s.w.grants.get = Mock(return_value=GetPermissionsResponse(privilege_assignments=[], next_page_token="same"))
    with pytest.raises(GovernanceError, match="token lặp"):
        s.grants(TARGET)


def test_effective_grants_preserve_inheritance():
    s = service()
    rows = grant_rows(s.grants(TARGET, effective=True), True)
    inherited = next(r for r in rows if r["Principal"] == "reporting")
    assert inherited["Privilege"] == "SELECT"
    assert inherited["Inherited from"] == "demo_governance.curated"


def test_view_cannot_receive_modify():
    s = service()
    s.w.tables.get = lambda **kw: {"table_type": "VIEW"}
    with pytest.raises(GovernanceError, match="chưa được hỗ trợ"):
        s.prepare(TARGET, "analysts", "grant", ["MODIFY"], "Ticket")


def test_api_failure_not_reported_as_success_and_no_secret_in_log(caplog):
    s = service()
    change = s.prepare(TARGET, "analysts", "grant", ["MODIFY"], "Approved ticket")
    s.w.grants.update.side_effect = RuntimeError("secret-access-token")
    with pytest.raises(RuntimeError):
        s.apply(change, TARGET.full_name)
    assert "secret-access-token" not in caplog.text
    assert "failed_or_unknown" in caplog.text


def test_actor_is_proxy_email_only():
    assert actor_from_headers({"X-Forwarded-Email": "Admin@Example.com"}) == "admin@example.com"
    assert actor_from_headers({"X-Forwarded-Preferred-Username": "admin@example.com"}) == ""


def test_read_scope_enforced():
    s = service()
    with pytest.raises(GovernanceError):
        s.metadata(Target("catalog", "production"))
