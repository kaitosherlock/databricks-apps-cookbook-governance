"""Test fixtures: a fake Databricks SDK that behaves like the real one.

The fake reproduces the response *shapes* that actually matter, because those
shapes are where the bugs live:

* ``grants.get`` returns privileges as plain strings, ``grants.get_effective``
  returns objects carrying inheritance provenance;
* a page can be empty and still carry a next-page token;
* ``update`` is a delta, so the fake applies add/remove rather than replacing.

Nothing here is importable by the application: fixtures live only in the test
tree, so no mock can leak into a user-facing path.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ucg.audit import OperationLog
from ucg.authz import Authorizer
from ucg.capabilities import CapabilityReport
from ucg.clients import Connection
from ucg.config import Settings
from ucg.identity import Actor, ExecutionIdentity
from ucg.services.base import Context


# -- SDK-shaped stand-ins ------------------------------------------------
class Box:
    """Object with an ``as_dict`` method, like every SDK model."""

    def __init__(self, **data):
        self._data = data
        for key, value in data.items():
            setattr(self, key, value)

    def as_dict(self):
        return dict(self._data)


@dataclass
class Page:
    privilege_assignments: list
    next_page_token: str | None = None


class FakeGrants:
    """Delta-semantics grants API with realistic direct/effective shapes."""

    def __init__(self, direct=None, inherited=None):
        # {full_name: {principal: set(privileges)}}
        self.direct = direct or {}
        # {full_name: [(principal, privilege, from_name, from_type)]}
        self.inherited = inherited or {}
        self.calls = []
        self.fail_with = None
        #: Set to inject a change between a preview and an apply.
        self.on_get = None

    def get(self, securable_type, full_name, max_results=None, page_token=None,
            principal=None):
        self.calls.append(("get", securable_type, full_name, max_results, page_token))
        if self.fail_with:
            raise self.fail_with
        if self.on_get:
            hook, self.on_get = self.on_get, None
            hook()
        if page_token == "page2":
            return Page([], None)
        rows = [
            Box(principal=who, privileges=sorted(privs))
            for who, privs in sorted(self.direct.get(full_name, {}).items())
            if privs
        ]
        # First page deliberately returns a token so the pagination loop is
        # exercised, including the legal empty second page.
        return Page(rows, "page2" if page_token is None else None)

    def get_effective(self, securable_type, full_name, max_results=None,
                      page_token=None, principal=None):
        self.calls.append(("get_effective", securable_type, full_name, max_results, page_token))
        if self.fail_with:
            raise self.fail_with
        if page_token is not None:
            return Page([], None)
        grouped: dict[str, list] = {}
        for who, privs in self.direct.get(full_name, {}).items():
            for priv in sorted(privs):
                # Direct entries omit the inheritance keys entirely.
                grouped.setdefault(who, []).append(Box(privilege=priv))
        for who, priv, from_name, from_type in self.inherited.get(full_name, []):
            grouped.setdefault(who, []).append(
                Box(privilege=priv, inherited_from_name=from_name,
                    inherited_from_type=from_type)
            )
        rows = [Box(principal=who, privileges=items) for who, items in sorted(grouped.items())]
        return Page(rows, None)

    def update(self, securable_type, full_name, changes=None):
        self.calls.append(("update", securable_type, full_name, changes))
        if self.fail_with:
            raise self.fail_with
        bucket = self.direct.setdefault(full_name, {})
        for change in changes or []:
            held = set(bucket.get(change.principal, set()))
            for priv in (change.add or []):
                held.add(getattr(priv, "value", priv))
            for priv in (change.remove or []):
                held.discard(getattr(priv, "value", priv))
            bucket[change.principal] = held
        return Box()


class FakeCatalogs:
    def __init__(self, items):
        self.items = items
        self.updated = []

    def list(self, include_browse=None, include_unbound=None, max_results=None,
             page_token=None):
        return iter(self.items)

    def get(self, name, include_browse=None):
        for item in self.items:
            if item.as_dict().get("name") == name:
                return item
        raise _not_found(name)

    def update(self, name, **kwargs):
        self.updated.append((name, kwargs))
        return Box(name=name, **kwargs)


class FakeSchemas:
    def __init__(self, items):
        self.items = items
        self.updated = []

    def list(self, catalog_name, include_browse=None, max_results=None, page_token=None):
        return iter([s for s in self.items
                     if s.as_dict().get("catalog_name") == catalog_name])

    def get(self, full_name, include_browse=None):
        for item in self.items:
            if item.as_dict().get("full_name") == full_name:
                return item
        raise _not_found(full_name)

    def update(self, full_name, **kwargs):
        self.updated.append((full_name, kwargs))
        return Box(full_name=full_name, **kwargs)


class FakeTables:
    def __init__(self, items):
        self.items = items

    def list(self, catalog_name, schema_name, **kwargs):
        return iter([t for t in self.items
                     if t.as_dict().get("catalog_name") == catalog_name
                     and t.as_dict().get("schema_name") == schema_name])

    def get(self, full_name, **kwargs):
        for item in self.items:
            if item.as_dict().get("full_name") == full_name:
                return item
        raise _not_found(full_name)


class FakeEmpty:
    """Any list/get surface that has nothing in it."""

    def __init__(self, error=None):
        self.error = error

    def _maybe_raise(self):
        if self.error:
            raise self.error

    def list(self, *a, **k):
        self._maybe_raise()
        return iter([])

    def list_credentials(self, *a, **k):
        return self.list()

    def list_shares(self, *a, **k):
        return self.list()

    def get(self, *a, **k):
        self._maybe_raise()
        raise _not_found("x")


class FakeCurrentUser:
    def __init__(self, name="app-4ee9bw adser"):
        self.name = name

    def me(self):
        return Box(user_name=self.name, display_name=self.name)


class FakeWorkspaceClient:
    def __init__(self, **overrides):
        self.grants = overrides.pop("grants", FakeGrants())
        self.catalogs = overrides.pop("catalogs", FakeCatalogs([]))
        self.schemas = overrides.pop("schemas", FakeSchemas([]))
        self.tables = overrides.pop("tables", FakeTables([]))
        self.volumes = overrides.pop("volumes", FakeEmpty())
        self.functions = overrides.pop("functions", FakeEmpty())
        self.registered_models = overrides.pop("registered_models", FakeEmpty())
        self.users = overrides.pop("users", FakeEmpty())
        self.groups = overrides.pop("groups", FakeEmpty())
        self.service_principals = overrides.pop("service_principals", FakeEmpty())
        self.current_user = overrides.pop("current_user", FakeCurrentUser())
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeDatabricksError(Exception):
    def __init__(self, error_code, message="upstream detail that must never be shown"):
        super().__init__(message)
        self.error_code = error_code


def _not_found(name):
    return FakeDatabricksError("NOT_FOUND", f"{name} not found")


# -- fixtures ------------------------------------------------------------
@pytest.fixture
def settings():
    return Settings(
        catalogs=frozenset({"catalog1"}),
        roles={"admin@x.com": "access_admin", "steward@x.com": "steward",
               "viewer@x.com": "viewer", "platform@x.com": "platform_admin"},
        enable_writes=True,
        environment="DEV",
    )


@pytest.fixture
def read_only_settings(settings):
    return Settings(
        catalogs=settings.catalogs, roles=dict(settings.roles),
        enable_writes=False, environment="DEV",
    )


def make_context(settings, client, email="admin@x.com") -> Context:
    actor = Actor(email=email, display_name=email, verified=False)
    connection = Connection(
        client=client,
        execution_identity=ExecutionIdentity.APP_SERVICE_PRINCIPAL,
        host="https://example.cloud.databricks.com",
    )
    return Context(
        settings=settings,
        actor=actor,
        connection=connection,
        authz=Authorizer(settings, email),
        capabilities=CapabilityReport(settings),
        log=OperationLog(),
    )


@pytest.fixture
def context_factory(settings):
    def build(client=None, email="admin@x.com", cfg=None):
        return make_context(cfg or settings, client or FakeWorkspaceClient(), email)
    return build


@pytest.fixture
def table_client():
    """A workspace with catalog1.schema1.routes and a few grants."""
    catalogs = FakeCatalogs([
        Box(name="catalog1", owner="owner@x.com", comment="Catalog thử nghiệm",
            catalog_type="MANAGED_CATALOG", browse_only=False),
        Box(name="other", owner="owner@x.com", comment="", browse_only=False),
    ])
    schemas = FakeSchemas([
        Box(name="schema1", catalog_name="catalog1", full_name="catalog1.schema1",
            owner="owner@x.com", comment="Schema thử nghiệm"),
    ])
    tables = FakeTables([
        Box(name="routes", catalog_name="catalog1", schema_name="schema1",
            full_name="catalog1.schema1.routes", owner="owner@x.com",
            table_type="MANAGED", comment="Bảng tuyến đường",
            columns=[{"name": "id", "type_text": "int", "nullable": False, "comment": ""}],
            properties={}, table_constraints=[]),
        Box(name="routes_view", catalog_name="catalog1", schema_name="schema1",
            full_name="catalog1.schema1.routes_view", owner="owner@x.com",
            table_type="VIEW", comment="", columns=[], properties={}),
    ])
    grants = FakeGrants(
        direct={
            "catalog1.schema1.routes": {"analysts": {"SELECT"}},
            "catalog1": {"analysts": {"USE_CATALOG"}},
        },
        inherited={
            "catalog1.schema1.routes": [
                ("analysts", "USE_CATALOG", "catalog1", "CATALOG"),
                ("engineers", "MODIFY", "catalog1.schema1", "SCHEMA"),
            ],
        },
    )
    return FakeWorkspaceClient(
        catalogs=catalogs, schemas=schemas, tables=tables, grants=grants,
    )
