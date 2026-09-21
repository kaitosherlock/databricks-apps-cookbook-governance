"""Authorization is enforced in the backend, not by hiding buttons."""
from __future__ import annotations

import pytest

from ucg.authz import Action, Authorizer
from ucg.config import Role, Settings
from ucg.errors import Code, GovernanceError
from ucg.naming import Target

TABLE = Target("table", "catalog1", "schema1", "routes")
OUT_OF_SCOPE = Target("table", "secret_catalog", "schema1", "routes")


def test_viewer_cannot_write_even_calling_the_backend_directly(settings):
    """A viewer who bypasses the UI still gets refused."""
    authz = Authorizer(settings, "viewer@x.com")
    assert authz.role == Role.VIEWER
    assert not authz.allows(Action.GRANT, TABLE)
    with pytest.raises(GovernanceError) as err:
        authz.require(Action.GRANT, TABLE)
    assert err.value.code == Code.FORBIDDEN_ROLE


def test_viewer_can_read(settings):
    authz = Authorizer(settings, "viewer@x.com")
    assert authz.allows(Action.READ_METADATA, TABLE)
    assert authz.allows(Action.READ_GRANTS, TABLE)


def test_steward_may_tag_but_not_grant(settings):
    authz = Authorizer(settings, "steward@x.com")
    assert authz.allows(Action.ASSIGN_TAG, TABLE)
    assert not authz.allows(Action.GRANT, TABLE)


def test_access_admin_may_grant_but_not_manage_sharing(settings):
    authz = Authorizer(settings, "admin@x.com")
    assert authz.allows(Action.GRANT, TABLE)
    assert authz.allows(Action.REVOKE, TABLE)
    assert not authz.allows(Action.MANAGE_SHARING, TABLE)


def test_platform_admin_may_manage_everything(settings):
    authz = Authorizer(settings, "platform@x.com")
    for action in (Action.GRANT, Action.MANAGE_SHARING, Action.MANAGE_POLICY,
                   Action.MANAGE_STORAGE, Action.MANAGE_BINDINGS):
        assert authz.allows(action, TABLE), action


def test_unknown_email_gets_the_default_role(settings):
    authz = Authorizer(settings, "stranger@x.com")
    assert authz.role == Role.VIEWER
    assert not authz.allows(Action.GRANT, TABLE)


def test_catalog_scope_blocks_out_of_scope_objects(settings):
    authz = Authorizer(settings, "platform@x.com")
    decision = authz.check(Action.GRANT, OUT_OF_SCOPE)
    assert not decision.allowed
    assert decision.code == Code.FORBIDDEN_SCOPE
    with pytest.raises(GovernanceError):
        authz.require_scope("secret_catalog")


def test_empty_catalog_allowlist_means_no_filter():
    settings = Settings(catalogs=frozenset(), roles={"a@x.com": Role.PLATFORM_ADMIN},
                        enable_writes=True)
    authz = Authorizer(settings, "a@x.com")
    assert authz.allows(Action.GRANT, OUT_OF_SCOPE)


def test_writes_disabled_blocks_every_mutation(read_only_settings):
    authz = Authorizer(read_only_settings, "platform@x.com")
    for action in sorted(Action.LABELS):
        decision = authz.check(action, TABLE)
        if action in ("read_metadata", "read_grants", "read_tags", "read_policies",
                      "read_storage", "read_sharing", "read_lineage", "read_quality",
                      "read_audit", "read_requests"):
            continue
        assert not decision.allowed, f"{action} should be blocked"
        assert decision.code == Code.WRITES_DISABLED


def test_local_mode_is_always_read_only():
    settings = Settings(roles={"a@x.com": Role.PLATFORM_ADMIN},
                        enable_writes=True, local=True)
    authz = Authorizer(settings, "a@x.com")
    decision = authz.check(Action.GRANT, TABLE)
    assert not decision.allowed
    assert decision.code == Code.WRITES_DISABLED


def test_missing_identity_blocks_mutations_but_not_reads(settings):
    authz = Authorizer(settings, "")
    assert authz.check(Action.GRANT, TABLE).code == Code.NO_IDENTITY
    # With no identity the role falls back to the default, which can still read.
    assert authz.allows(Action.READ_METADATA, TABLE)


def test_self_approval_is_blocked(settings):
    authz = Authorizer(settings, "admin@x.com")
    with pytest.raises(GovernanceError) as err:
        authz.require_not_self("admin@x.com")
    assert err.value.code == Code.SELF_APPROVAL
    authz.require_not_self("someone-else@x.com")


def test_allowed_actions_explains_why_each_is_blocked(settings):
    authz = Authorizer(settings, "viewer@x.com")
    actions = authz.allowed_actions(TABLE)
    assert actions[Action.GRANT]["allowed"] is False
    assert actions[Action.GRANT]["reason"]
    assert actions[Action.READ_GRANTS]["allowed"] is True


def test_role_escalation_via_duplicate_entries_takes_the_highest():
    settings = Settings.from_env({
        "GOVERNANCE_ROLES": "a@x.com:viewer,a@x.com:platform_admin",
    })
    assert settings.role_for("a@x.com") == Role.PLATFORM_ADMIN


def test_unknown_role_string_is_dropped_not_guessed():
    settings = Settings.from_env({"GOVERNANCE_ROLES": "a@x.com:superuser"})
    assert settings.role_for("a@x.com") == Role.VIEWER


def test_legacy_admin_allowlist_maps_to_access_admin():
    settings = Settings.from_env({"GOVERNANCE_ADMIN_EMAILS": "Old@X.com"})
    assert settings.role_for("old@x.com") == Role.ACCESS_ADMIN
