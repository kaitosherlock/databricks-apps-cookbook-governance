"""Render the real Streamlit pages against a fake workspace.

These are the checks that a unit test of the service layer cannot make: that a
page renders at all, that a viewer is shown an explanation instead of a write
control, and that a refused read does not blank the screen.
"""
from __future__ import annotations

import pytest

from conftest import FakeDatabricksError, FakeWorkspaceClient, make_context

st_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = st_testing.AppTest

from ucg.naming import Target  # noqa: E402
from ui import state  # noqa: E402

TABLE = Target("table", "catalog1", "schema1", "routes")


def _script(render=None, ctx=None, selection=None, before=None):
    """Body executed inside the Streamlit test runtime.

    AppTest re-executes this function's source in a fresh module namespace, so
    it must close over nothing: every dependency arrives as a keyword argument
    and every import happens inside.
    """
    import streamlit as st

    from ui import state as page_state

    if selection:
        st.session_state[page_state.SELECTION] = dict(selection)
    if before is not None:
        before(page_state, st)
    render(ctx)


def run_page(render, ctx, *, selection=None, before=None):
    app = AppTest.from_function(
        _script,
        kwargs={"render": render, "ctx": ctx, "selection": selection, "before": before},
        default_timeout=30,
    )
    app.run()
    return app


def all_text(app) -> str:
    chunks = []
    for collection in (app.markdown, app.caption, app.info, app.warning,
                       app.error, app.success, app.subheader, app.title):
        for element in collection:
            chunks.append(str(getattr(element, "value", "")))
    return "\n".join(chunks)


SELECTED_TABLE = {"catalog": "catalog1", "schema": "schema1",
                  "kind": "table", "name": "routes"}


# -- home ----------------------------------------------------------------
def test_home_renders_and_lists_assets(settings, table_client):
    from ui.pages import home

    app = run_page(home.render, make_context(settings, table_client))
    assert not app.exception
    text = all_text(app)
    assert "Tìm tài sản dữ liệu" in text


def test_home_explains_an_empty_workspace(settings):
    from ui.pages import home

    app = run_page(home.render, make_context(settings, FakeWorkspaceClient()))
    assert not app.exception
    text = all_text(app)
    # An empty catalog list must come with instructions, not a blank screen.
    assert "catalog" in text.lower()


# -- asset ---------------------------------------------------------------
def test_asset_page_shows_identity_and_owner(settings, table_client):
    from ui.pages import asset

    app = run_page(asset.render, make_context(settings, table_client),
                   selection=SELECTED_TABLE)
    assert not app.exception
    text = all_text(app)
    assert "routes" in text
    assert "Chủ sở hữu" in text


# -- permissions ---------------------------------------------------------
def test_permissions_distinguishes_direct_from_inherited(settings, table_client):
    from ui.pages import permissions

    app = run_page(permissions.render, make_context(settings, table_client),
                   selection=SELECTED_TABLE)
    assert not app.exception
    frames = [df.value for df in app.dataframe]
    assert frames, "the grant table should render"
    rendered = str(frames)
    assert "Cấp trực tiếp" in rendered
    assert "Kế thừa" in rendered


def test_permissions_explains_a_refused_read(settings, table_client):
    from ui.pages import permissions

    table_client.grants.fail_with = FakeDatabricksError("PERMISSION_DENIED")
    app = run_page(permissions.render, make_context(settings, table_client),
                   selection=SELECTED_TABLE)
    assert not app.exception
    text = all_text(app)
    assert "quyền" in text.lower()
    # A refusal must never be phrased as "nobody has access".
    assert "Không ai có quyền" not in text


# -- change access -------------------------------------------------------
def test_viewer_sees_read_only_with_a_reason(settings, table_client):
    from ui.pages import change_access

    ctx = make_context(settings, table_client, "viewer@x.com")
    app = run_page(change_access.render, ctx, selection=SELECTED_TABLE)
    assert not app.exception
    text = all_text(app)
    assert "Chỉ đọc" in text
    assert "Người xem" in text or "vai trò" in text.lower()
    # No apply control may be offered at all.
    labels = [b.label for b in app.button]
    assert not any("Áp dụng" in label for label in labels)


def test_writes_disabled_explains_the_deploy_requirement(read_only_settings, table_client):
    from ui.pages import change_access

    ctx = make_context(read_only_settings, table_client, "admin@x.com")
    app = run_page(change_access.render, ctx, selection=SELECTED_TABLE)
    assert not app.exception
    text = all_text(app)
    assert "GOVERNANCE_ENABLE_WRITES" in text
    assert "deploy" in text.lower()


def test_admin_sees_the_preview_step(settings, table_client):
    from ui.pages import change_access

    ctx = make_context(settings, table_client, "admin@x.com")
    app = run_page(change_access.render, ctx, selection=SELECTED_TABLE)
    assert not app.exception
    labels = [b.label for b in app.button]
    assert any("xem trước" in label.lower() for label in labels)


def test_a_stale_preview_is_discarded_when_an_input_changes(settings, table_client):
    """The rule that stops a Streamlit rerun applying an outdated plan."""
    from ucg.services.grants import GrantService

    from ui.pages import change_access

    ctx = make_context(settings, table_client, "admin@x.com")
    plan = GrantService(ctx).plan_change(TABLE, "newcomer", "grant", ["SELECT"], "lý do")

    def stage_a_stale_plan(page_state, st):
        page_state.set_plan(plan, "signature-built-from-the-old-inputs")
        # A different signature stands for "the operator edited a field".
        page_state.invalidate_plan_if_changed("signature-after-the-edit")
        st.session_state["_plan_survived"] = page_state.get_plan() is not None

    app = run_page(change_access.render, ctx, selection=SELECTED_TABLE,
                   before=stage_a_stale_plan)
    assert not app.exception
    assert app.session_state["_plan_survived"] is False


# -- diagnostics ---------------------------------------------------------
def test_diagnostics_lists_capabilities_and_role(settings, table_client):
    from ui.pages import diagnostics

    app = run_page(diagnostics.render, make_context(settings, table_client))
    assert not app.exception
    text = all_text(app)
    assert "Khả năng" in text


def test_diagnostics_never_prints_a_secret(settings, table_client, monkeypatch):
    from ui.pages import diagnostics

    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "super-secret-value")
    app = run_page(diagnostics.render, make_context(settings, table_client))
    assert not app.exception
    rendered = all_text(app) + str([d.value for d in app.dataframe])
    assert "super-secret-value" not in rendered


# -- activity ------------------------------------------------------------
def test_activity_labels_itself_as_session_only(settings, table_client):
    from ui.pages import activity

    app = run_page(activity.render, make_context(settings, table_client))
    assert not app.exception
    text = all_text(app)
    assert "phiên" in text.lower()
    assert "system.access.audit" in text


def test_activity_flags_an_unknown_outcome(settings, table_client):
    from ucg.audit import Event, Status
    from ui.pages import activity

    ctx = make_context(settings, table_client, "admin@x.com")
    ctx.log.record(Event(
        action="grant", target="catalog1.schema1.routes", target_type="Bảng",
        actor="admin@x.com", execution_identity="app_service_principal",
        status=Status.UNKNOWN, summary="Cấp quyền SELECT",
    ))
    app = run_page(activity.render, ctx)
    assert not app.exception
    text = all_text(app)
    assert "chưa xác định" in text.lower()
    assert "trước khi thử lại" in text.lower() or "đối chiếu" in text.lower()
