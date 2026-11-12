"""Nested contexts keep the original database error, restore the selected tenant and set GUCs once (#237)."""

from uuid import uuid4

import pytest
from django.db import DatabaseError, DataError, connection
from django.test.utils import CaptureQueriesContext

from skeleton.tenancy.context import InvalidContext, current_context, request_context, system_context, tenant_context
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.usefixtures("skeleton_db")


@pytest.fixture
def fixture():
    return build_security_fixture()


def setting(name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", ["skeleton." + name])
        return cursor.fetchone()[0]


def test_database_error_inside_nested_context_keeps_original_exception(fixture):
    with request_context(actor=fixture.alice, request_id=uuid4(), source="test:context-outer"):
        with pytest.raises(DataError):
            with tenant_context(organization_id=fixture.a.pk, actor=fixture.alice, source="test:context"):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1/0")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
        assert setting("tenant_id") == ""
        assert current_context().organization_id is None
    assert connection.connection is not None


def test_python_exception_inside_nested_context_restores_gucs(fixture):
    with request_context(actor=fixture.alice, request_id=uuid4(), source="test:context-outer"):
        with pytest.raises(RuntimeError):
            with tenant_context(organization_id=fixture.a.pk, actor=fixture.alice, source="test:context"):
                raise RuntimeError("boom")
        assert setting("tenant_id") == ""
        assert setting("user_id") == str(fixture.alice.pk)


def test_swallowed_database_error_does_not_close_the_connection(fixture):
    with request_context(actor=fixture.alice, request_id=uuid4(), source="test:context-outer"):
        with pytest.raises(DatabaseError):
            with tenant_context(organization_id=fixture.a.pk, actor=fixture.alice, source="test:context"):
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1/0")
                except DataError:
                    pass
        assert connection.connection is not None


def test_successful_nested_context_reapplies_prior_gucs(fixture):
    with request_context(actor=fixture.alice, request_id=uuid4(), source="test:context-outer"):
        with tenant_context(organization_id=fixture.a.pk, actor=fixture.alice, source="test:context-inner"):
            assert setting("tenant_id") == str(fixture.a.pk)
        assert setting("tenant_id") == ""
        assert setting("source") == "test:context-outer"


def test_top_level_system_context_does_not_leak_selected_tenant(fixture):
    for organization in (fixture.a, fixture.b):
        with system_context(reason="test:selected"):
            with system_context(reason="test:selected", organization_id=organization.pk):
                assert current_context().organization_id == organization.pk


def test_request_still_selects_a_single_tenant(fixture):
    with request_context(actor=fixture.alice, request_id=uuid4(), source="test:single-tenant"):
        with tenant_context(organization_id=fixture.a.pk, actor=fixture.alice, source="test:a"):
            pass
        with pytest.raises(InvalidContext):
            with tenant_context(organization_id=fixture.b.pk, actor=fixture.alice, source="test:b"):
                pass


def test_context_entry_sets_gucs_in_one_statement(fixture):
    with CaptureQueriesContext(connection) as captured:
        with actor_tenant(fixture.alice, fixture.a):
            pass
    assert sum("set_config" in query["sql"] for query in captured) == 3


def test_middleware_resolves_the_url_once(fixture, monkeypatch):
    from django.test import Client
    from django.urls import resolve

    from skeleton.tenancy import middleware

    calls = []

    def counting(path, *args, **kwargs):
        calls.append(path)
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(middleware, "resolve", counting)
    client = Client()
    client.force_login(fixture.alice)
    assert client.get(f"/o/{fixture.a.pk}/").status_code == 200
    assert len(calls) == 1
