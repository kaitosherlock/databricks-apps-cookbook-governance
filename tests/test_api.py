"""The HTTP layer, with the Databricks SDK faked out.

These tests exist for one reason: moving from Streamlit to a browser client
changed *where* the safety rules are enforced, and the rules themselves must not
have moved with it. Specifically:

* a Plan must never be reachable, readable or forgeable from the client;
* the actor must come from proxy headers on every request, never from a body;
* a listing must keep saying whether it was complete;
* an upstream error must never reach the wire with its response body attached.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import FakeDatabricksError, FakeWorkspaceClient, make_context

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "governance-app"))

import server  # noqa: E402
from api import runtime  # noqa: E402
from api.http import get_context  # noqa: E402
from ucg.audit import Status  # noqa: E402
from ucg.errors import Code  # noqa: E402

ADMIN = "admin@x.com"
VIEWER = "viewer@x.com"
OTHER_ADMIN = "platform@x.com"


@pytest.fixture(autouse=True)
def clean_plan_store():
    """The plan store is process-level, so a leaked plan would cross tests."""
    for email in (ADMIN, VIEWER, OTHER_ADMIN):
        runtime.drop_plans_for(email)
    yield
    for email in (ADMIN, VIEWER, OTHER_ADMIN):
        runtime.drop_plans_for(email)


@pytest.fixture
def api(settings, table_client):
    """A client whose request context is the fake workspace."""
    def build(email=ADMIN, client=None, cfg=None):
        ctx = make_context(cfg or settings, client or table_client, email)
        server.app.dependency_overrides[get_context] = lambda: ctx
        return TestClient(server.app, raise_server_exceptions=False), ctx

    yield build
    server.app.dependency_overrides.clear()


TABLE_Q = {
    "kind": "table", "catalog": "catalog1",
    "schema": "schema1", "name": "routes",
}


# -- session: the shell that every screen renders ------------------------
def test_session_reports_environment_and_mode_on_one_endpoint(api):
    """The PROD marker is shell state, not something a page opts into.

    In the Streamlit build this data was drawn by whichever page remembered to
    call chrome.render_banner, which is why it appeared on exactly one screen.
    """
    client, _ = api()
    body = client.get("/api/session").json()

    assert body["environment"]["name"] == "DEV"
    assert body["environment"]["high_risk"] is False
    assert body["mode"]["writable"] is True
    assert body["actor"]["role"] == "access_admin"
    assert body["scope"]["catalogs"] == ["catalog1"]


def test_session_flags_production_as_high_risk(api, settings):
    from ucg.config import Settings

    prod = Settings(
        catalogs=settings.catalogs, roles=dict(settings.roles),
        enable_writes=True, environment="PROD",
    )
    client, _ = api(cfg=prod)
    env = client.get("/api/session").json()["environment"]

    assert env["high_risk"] is True
    assert "PROD" in env["label"]
    # The warning is words, not a colour: it has to survive a greyscale
    # screenshot and a colour-blind reader.
    assert env["warning"]


def test_viewer_session_is_read_only_with_a_reason(api):
    client, _ = api(email=VIEWER)
    mode = client.get("/api/session").json()["mode"]

    assert mode["writable"] is False
    assert "Người xem" in mode["reason"]


# -- listings keep their honesty fields ----------------------------------
def test_listing_carries_completeness(api):
    client, _ = api()
    body = client.get("/api/assets/catalogs").json()

    assert body["completeness"] == "complete"
    assert body["observed_at"]
    assert [c["name"] for c in body["items"]] == ["catalog1"]


def test_search_refuses_kinds_it_does_not_recognise(api):
    """An unknown kind is refused rather than silently ignored.

    Silently dropping it would run the search over the default kind and present
    the result as an answer to a question nobody asked.
    """
    client, _ = api()
    response = client.get("/api/assets/search", params={
        "catalog": "catalog1", "schema": "schema1", "kinds": ["nonsense"],
    })
    assert response.status_code == 400
    assert response.json()["error"]["code"] == Code.INVALID_INPUT


# -- grants: direct vs inherited stays visible ---------------------------
def test_effective_grants_expose_provenance(api):
    client, _ = api()
    body = client.get("/api/grants", params={**TABLE_Q, "effective": True}).json()
    rows = {(r["principal"], r["privilege"]): r for r in body["rows"]}

    direct = rows[("analysts", "SELECT")]
    assert direct["source"] == "direct"
    assert direct["revocable_here"] is True

    inherited = rows[("analysts", "USE_CATALOG")]
    assert inherited["source"] == "inherited"
    assert inherited["inherited_from"] == "catalog1"
    # An inherited grant cannot be revoked here, so the client must not be
    # able to render a button for it.
    assert inherited["revocable_here"] is False


def test_owner_row_is_marked_as_ownership_not_a_grant(api):
    client, _ = api()
    body = client.get("/api/grants", params={**TABLE_Q, "effective": False}).json()
    owner_rows = [r for r in body["rows"] if r["source"] == "ownership"]

    assert owner_rows, "the owner holds everything implicitly and must be shown"
    assert owner_rows[0]["principal"] == "owner@x.com"
    assert owner_rows[0]["revocable_here"] is False


def test_revocable_lists_only_direct_grants(api):
    client, _ = api()
    body = client.get("/api/grants/revocable", params=TABLE_Q).json()

    assert body["can_revoke"] is True
    assert [p["principal"] for p in body["principals"]] == ["analysts"]
    assert body["principals"][0]["privileges"] == ["SELECT"]


def test_privileges_endpoint_carries_risk_and_meaning(api):
    client, _ = api()
    body = client.get("/api/grants/privileges", params=TABLE_Q).json()
    codes = {p["code"]: p for p in body["privileges"]}

    assert "SELECT" in codes
    assert codes["SELECT"]["explanation"]
    assert codes["SELECT"]["risk"] in ("low", "medium", "high")


# -- the plan pipeline ---------------------------------------------------
def _plan(client, **overrides):
    body = {
        **TABLE_Q,
        "action": "grant",
        "principal": "engineers",
        "privileges": ["SELECT"],
        "reason": "TICKET-123",
    }
    body.update(overrides)
    return client.post("/api/grants/plan", json=body)


def test_plan_returns_a_preview_without_changing_anything(api):
    client, ctx = api()
    response = _plan(client)
    assert response.status_code == 200
    body = response.json()

    assert body["plan_id"]
    assert body["preview"], "a preview with no lines is not a preview"
    assert body["confirm_with"] == "catalog1.schema1.routes"
    # Nothing was sent to Databricks.
    assert not [c for c in ctx.w.grants.calls if c[0] == "update"]


def test_plan_never_puts_the_change_in_the_response(api):
    """The client gets something to read, not something to replay.

    The principal and privilege list stay server-side: /apply reads them back
    from the stored plan, so a tampered client cannot swap them.
    """
    client, _ = api()
    body = _plan(client).json()

    assert "payload" not in body
    assert "principal" not in body
    assert "privileges" not in body
    assert "before" not in body


def test_apply_executes_the_stored_plan(api):
    client, ctx = api()
    plan_id = _plan(client).json()["plan_id"]

    response = client.post("/api/grants/apply", json={
        "plan_id": plan_id, "confirmation": "catalog1.schema1.routes",
    })
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == Status.VERIFIED
    assert body["succeeded"] is True
    # Verified by reading back, not by echoing the request.
    assert body["verified"]["applied"] is True
    assert "SELECT" in body["verified"]["privileges_now"]
    assert ctx.w.grants.direct["catalog1.schema1.routes"]["engineers"] == {"SELECT"}


def test_apply_refuses_a_wrong_confirmation(api):
    client, ctx = api()
    plan_id = _plan(client).json()["plan_id"]

    response = client.post("/api/grants/apply", json={
        "plan_id": plan_id, "confirmation": "catalog1.schema1.wrong",
    })
    assert response.status_code == 400
    assert not [c for c in ctx.w.grants.calls if c[0] == "update"]


def test_a_plan_cannot_be_applied_twice(api):
    client, _ = api()
    plan_id = _plan(client).json()["plan_id"]
    confirm = {"plan_id": plan_id, "confirmation": "catalog1.schema1.routes"}

    assert client.post("/api/grants/apply", json=confirm).status_code == 200
    second = client.post("/api/grants/apply", json=confirm)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == Code.STALE_PLAN


def test_one_operator_cannot_apply_another_operators_plan(api, settings, table_client):
    """Plan ids are opaque, but guessing one must still get you nowhere."""
    client, _ = api(email=ADMIN)
    plan_id = _plan(client).json()["plan_id"]

    # A different admin, same workspace, holding a valid id.
    other = make_context(settings, table_client, OTHER_ADMIN)
    server.app.dependency_overrides[get_context] = lambda: other
    response = client.post("/api/grants/apply", json={
        "plan_id": plan_id, "confirmation": "catalog1.schema1.routes",
    })

    assert response.status_code == 409
    assert response.json()["error"]["code"] == Code.STALE_PLAN
    assert not [c for c in table_client.grants.calls if c[0] == "update"]


def test_apply_rejects_an_unknown_plan_id(api):
    client, _ = api()
    response = client.post("/api/grants/apply", json={
        "plan_id": "0" * 32, "confirmation": "catalog1.schema1.routes",
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == Code.STALE_PLAN


def test_state_change_between_preview_and_apply_aborts(api):
    """The revalidate step: refuse rather than act on a stale view."""
    client, ctx = api()
    plan_id = _plan(client).json()["plan_id"]

    # Someone else grants the same privilege in the gap.
    def interfere():
        ctx.w.grants.direct["catalog1.schema1.routes"]["engineers"] = {"SELECT"}

    ctx.w.grants.on_get = interfere

    response = client.post("/api/grants/apply", json={
        "plan_id": plan_id, "confirmation": "catalog1.schema1.routes",
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == Code.STALE_PLAN


def test_viewer_cannot_build_a_plan(api):
    client, ctx = api(email=VIEWER)
    response = _plan(client)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.FORBIDDEN_ROLE
    assert not [c for c in ctx.w.grants.calls if c[0] == "update"]


def test_writes_disabled_blocks_a_plan_whatever_the_role(api, read_only_settings):
    client, _ = api(email=ADMIN, cfg=read_only_settings)
    response = _plan(client)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.WRITES_DISABLED


def test_out_of_scope_catalog_is_refused(api):
    client, _ = api()
    response = _plan(client, catalog="other", schema="schema1", name="routes")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.FORBIDDEN_SCOPE


def test_revoking_an_inherited_privilege_is_refused(api):
    """engineers hold MODIFY on this table by inheritance from the schema.

    MODIFY is a perfectly valid privilege on a table, so this reaches the check
    that matters: a grant inherited from a parent cannot be revoked here, and
    the API says so instead of sending a call that would not do what was asked.
    """
    client, ctx = api()
    response = _plan(
        client, action="revoke", principal="engineers", privileges=["MODIFY"],
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == Code.INVALID_INPUT
    assert "kế thừa" in error["message"].lower()
    assert not [c for c in ctx.w.grants.calls if c[0] == "update"]


def test_privilege_invalid_for_the_object_type_is_refused(api):
    """USE_CATALOG is not grantable on a table at all, at any source."""
    client, _ = api()
    response = _plan(client, privileges=["USE_CATALOG"])

    assert response.status_code == 400
    assert response.json()["error"]["code"] == Code.INVALID_PRIVILEGE


def test_building_a_new_plan_discards_the_previous_one(api):
    """One pending preview per operator, as the old sidebar promised."""
    client, _ = api()
    first = _plan(client).json()["plan_id"]
    second = _plan(client, privileges=["MODIFY"]).json()["plan_id"]

    assert first != second
    assert runtime.get_plan(first) is None

    response = client.post("/api/grants/apply", json={
        "plan_id": first, "confirmation": "catalog1.schema1.routes",
    })
    assert response.status_code == 409


def test_cancelling_a_plan_makes_it_unusable(api):
    client, _ = api()
    plan_id = _plan(client).json()["plan_id"]

    assert client.delete(f"/api/grants/plan/{plan_id}").status_code == 200
    assert runtime.get_plan(plan_id) is None


# -- errors never leak the upstream body ---------------------------------
def test_upstream_error_body_is_never_returned(api, settings):
    leaky = FakeDatabricksError(
        "PERMISSION_DENIED", "token=dapi-SECRET-do-not-leak")
    client_obj = FakeWorkspaceClient()
    client_obj.grants.fail_with = leaky

    client, _ = api(client=client_obj)
    response = client.get("/api/grants", params=TABLE_Q)

    assert response.status_code == 403
    body = response.text
    assert "SECRET" not in body
    assert "dapi" not in body

    error = response.json()["error"]
    assert error["code"] == Code.PERMISSION_DENIED
    assert error["next_step"]
    assert error["correlation_id"]


def test_unknown_api_path_is_a_json_404_not_the_spa(api):
    client, _ = api()
    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == Code.NOT_FOUND


# -- identity comes from headers, never from the client ------------------
def test_actor_is_read_from_proxy_headers():
    from api.context import resolve_actor

    actor = resolve_actor({
        "x-forwarded-email": "Someone@X.com",
        "x-forwarded-preferred-username": "Someone",
    })
    assert actor.email == "someone@x.com"
    assert actor.verified is False
    # Without a forwarded token we state the weaker guarantee, not the stronger.
    assert "proxy" in actor.trust_note.lower()


def test_actor_is_absent_when_the_proxy_sends_nothing():
    from api.context import resolve_actor

    assert resolve_actor({}).present is False


def test_no_identity_is_a_401_with_a_stable_code(settings, table_client):
    """A missing identity is a setup problem, and the UI renders it as one."""
    from api import context as ctx_module

    server.app.dependency_overrides.clear()

    def no_identity(headers):
        raise ctx_module.NoIdentity()

    original, ctx_module.build = ctx_module.build, no_identity
    try:
        client = TestClient(server.app, raise_server_exceptions=False)
        response = client.get("/api/session")
    finally:
        ctx_module.build = original

    assert response.status_code == 401
    assert response.json()["error"]["code"] == Code.NO_IDENTITY


def test_health_does_not_require_an_identity():
    server.app.dependency_overrides.clear()
    response = TestClient(server.app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True


# -- serving the compiled front end --------------------------------------
class TestStaticServing:
    """The SPA fallback, which is what makes client-side routing work.

    Databricks Apps runs one process, so the same server that answers /api also
    hands out the built UI. Getting the fallback wrong is not cosmetic: a route
    that falls through to the root document silently redirects the operator back
    to the search page instead of opening what they asked for.
    """

    @staticmethod
    def _build(tmp_path, monkeypatch):
        static = tmp_path / "static"
        (static / "quyen").mkdir(parents=True)
        (static / "_next" / "static").mkdir(parents=True)
        (static / "index.html").write_text("<html>shell</html>", encoding="utf-8")
        (static / "quyen" / "index.html").write_text("<html>quyen</html>", encoding="utf-8")
        (static / "_next" / "static" / "app.js").write_text("//js", encoding="utf-8")

        monkeypatch.setattr(server, "STATIC_DIR", static)
        monkeypatch.setattr(server, "INDEX", static / "index.html")
        server.app.dependency_overrides.clear()
        return TestClient(server.app, raise_server_exceptions=False)

    def test_route_directory_serves_its_own_index(self, tmp_path, monkeypatch):
        """Built with trailingSlash, so /quyen/ is a directory holding index.html."""
        client = self._build(tmp_path, monkeypatch)
        response = client.get("/quyen/")

        assert response.status_code == 200
        assert "quyen" in response.text

    def test_unknown_route_falls_back_to_the_shell(self, tmp_path, monkeypatch):
        """The front end owns its routes, so an unknown path is not a 404."""
        client = self._build(tmp_path, monkeypatch)
        response = client.get("/khong-ton-tai/")

        assert response.status_code == 200
        assert "shell" in response.text

    def test_a_real_file_is_served_as_itself(self, tmp_path, monkeypatch):
        client = self._build(tmp_path, monkeypatch)
        response = client.get("/_next/static/app.js")

        assert response.status_code == 200
        assert "//js" in response.text

    def test_path_traversal_cannot_escape_the_static_root(self, tmp_path, monkeypatch):
        secret = tmp_path / "secret.txt"
        secret.write_text("do not serve me", encoding="utf-8")
        client = self._build(tmp_path, monkeypatch)

        for attempt in ("/../secret.txt", "/..%2fsecret.txt", "/a/../../secret.txt"):
            response = client.get(attempt)
            assert "do not serve me" not in response.text, attempt

    def test_api_paths_are_never_answered_with_html(self, tmp_path, monkeypatch):
        client = self._build(tmp_path, monkeypatch)
        response = client.get("/api/nope")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == Code.NOT_FOUND

    def test_missing_build_explains_itself(self, tmp_path, monkeypatch):
        """Running the server before `npm run build` must say so, not 500."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(server, "STATIC_DIR", empty)
        monkeypatch.setattr(server, "INDEX", empty / "index.html")
        server.app.dependency_overrides.clear()
        response = TestClient(server.app).get("/")

        assert response.status_code == 503
        assert "npm run build" in response.text

    def test_api_responses_are_never_cached(self, api):
        """Governance data is per-identity and must not sit in a shared cache."""
        client, _ = api()
        response = client.get("/api/session")

        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
