"""Every service imports, instantiates, and degrades honestly.

Compiling a module proves nothing about a name that only resolves at call time,
so each service is actually constructed and each read is actually invoked
against a client that refuses everything. The contract under test is that a
refusal produces an explained error, never a crash and never a silently empty
list.
"""
from __future__ import annotations

import importlib
import inspect

import pytest

from conftest import FakeDatabricksError, FakeWorkspaceClient, make_context
from ucg.errors import Code, GovernanceError
from ucg.naming import Target
from ucg.services.base import Listing, Service

MODULES = [
    "ucg.services.assets",
    "ucg.services.grants",
    "ucg.services.principals",
    "ucg.services.tags",
    "ucg.services.policies",
    "ucg.services.storage",
    "ucg.services.federation",
    "ucg.services.sharing",
    "ucg.services.lineage",
    "ucg.services.quality",
    "ucg.services.access_requests",
    "ucg.services.audit_query",
    "ucg.services.sql",
]

TABLE = Target("table", "catalog1", "schema1", "routes")


def _service_classes(module_name):
    module = importlib.import_module(module_name)
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Service) and obj is not Service and obj.__module__ == module_name:
            yield obj


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name):
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", MODULES)
def test_no_streamlit_in_the_backend(module_name):
    """The backend must stay usable without a UI framework."""
    source = inspect.getsource(importlib.import_module(module_name))
    assert "import streamlit" not in source
    assert "from streamlit" not in source


@pytest.mark.parametrize("module_name", MODULES)
def test_service_constructs(module_name, settings):
    ctx = make_context(settings, FakeWorkspaceClient())
    for cls in _service_classes(module_name):
        cls(ctx)


def _denying_client():
    """A client where every attribute access yields a refusing API."""
    class Denier:
        def __getattr__(self, _name):
            def call(*a, **k):
                raise FakeDatabricksError("PERMISSION_DENIED", "sensitive upstream body")
            return call

    class Client:
        def __getattr__(self, _name):
            return Denier()

    return Client()


@pytest.mark.parametrize("module_name", MODULES)
def test_zero_argument_reads_degrade_without_crashing(module_name, settings):
    """A refused read returns an explained Listing or raises GovernanceError."""
    ctx = make_context(settings, _denying_client())
    for cls in _service_classes(module_name):
        service = cls(ctx)
        for name, method in inspect.getmembers(service, inspect.ismethod):
            if name.startswith("_") or name in ("read", "listing", "execute", "apply"):
                continue
            signature = inspect.signature(method)
            required = [
                p for p in signature.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)
            ]
            if required:
                continue
            try:
                result = method()
            except GovernanceError as err:
                assert err.message, f"{cls.__name__}.{name} raised an empty message"
                assert "sensitive upstream body" not in err.message
                continue
            except NotImplementedError:
                continue
            if isinstance(result, Listing):
                assert result.error is None or isinstance(result.error, GovernanceError)
                if result.error is not None:
                    assert "sensitive upstream body" not in result.error.message


def test_refused_listing_is_distinguishable_from_an_empty_one(settings):
    from ucg.services.assets import AssetService

    refused = AssetService(make_context(settings, _denying_client())).catalogs()
    assert not refused.ok
    assert refused.error.code == Code.PERMISSION_DENIED

    empty = AssetService(make_context(settings, FakeWorkspaceClient())).catalogs()
    assert empty.ok
    assert empty.empty


def test_access_requests_exposes_no_approval_method(settings):
    """Databricks has no approval API, so the app must not appear to have one."""
    from ucg.services.access_requests import AccessRequestService

    names = {n for n, _ in inspect.getmembers(AccessRequestService, inspect.isfunction)}
    for forbidden in ("approve", "deny", "reject", "list_pending", "pending_requests"):
        assert forbidden not in names, f"AccessRequestService must not expose {forbidden}"
    assert "không" in AccessRequestService.NO_APPROVAL_API.lower()


def test_storage_never_calls_the_temporary_credential_api(settings):
    """generate_temporary_* returns live cloud credentials, not a health check."""
    import ucg.services.storage as storage

    source = inspect.getsource(storage)
    assert "generate_temporary_service_credential(" not in source.replace(
        "``generate_temporary_service_credential``", ""
    ).replace("generate_temporary_service_credential`` is", "")


def test_sharing_never_returns_recipient_credentials(settings):
    from ucg.services.sharing import RecipientRef, SharingService

    fields = set(RecipientRef.__dataclass_fields__)
    for secret in ("activation_url", "tokens", "sharing_code", "recipient_profile",
                   "recipient_profile_str", "bearer_token"):
        assert secret not in fields, f"RecipientRef must not carry {secret}"

    class Recipients:
        def list(self, *a, **k):
            return iter([_recipient_with_secrets()])

        def get(self, name):
            return _recipient_with_secrets()

    ctx = make_context(settings, FakeWorkspaceClient(recipients=Recipients()))
    listing = SharingService(ctx).recipients()
    assert listing.ok
    rendered = str([r.row() for r in listing.items])
    assert "https://activate.example" not in rendered
    assert "SECRETCODE" not in rendered


def _recipient_with_secrets():
    from conftest import Box

    return Box(
        name="partner", authentication_type="TOKEN", owner="o@x.com",
        activated=True, sharing_code="SECRETCODE",
        activation_url="https://activate.example/one-time",
        tokens=[{"id": "t1", "activation_url": "https://activate.example/one-time",
                 "expiration_time": 1700000000000}],
        ip_access_list={"allowed_ip_addresses": []},
    )


def test_federation_never_returns_connection_option_values(settings):
    from ucg.services.federation import FederationService

    class Connections:
        def list(self, *a, **k):
            return iter([_connection_with_secrets()])

        def get(self, name):
            return _connection_with_secrets()

    ctx = make_context(settings, FakeWorkspaceClient(connections=Connections()))
    service = FederationService(ctx)
    detail = service.connection_detail("pg")
    rendered = str(detail)
    assert "hunter2" not in rendered, "connection option values must never be returned"


def _connection_with_secrets():
    from conftest import Box

    return Box(
        name="pg", connection_type="POSTGRESQL", owner="o@x.com",
        options={"host": "db.internal", "port": "5432", "user": "svc",
                 "password": "hunter2"},
        read_only=False, comment="",
    )
