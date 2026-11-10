"""Service API base inside real tenant contexts (#97): actor, tenant and activation fail closed."""

import pytest
from django.db import connection

from skeleton.access.policies import authorized_queryset
from skeleton.service_api import Forbidden, Page, ServiceAPI, Unavailable, paginate, service_method
from skeleton.system.applications import application_state, configure_application
from skeleton.tenancy.context import system_context
from skeleton.testing.applications import activation_revision
from tools.security_fixtures import actor_tenant, build_security_fixture


class NoteTitles(ServiceAPI):
    app_label = "example_notes"

    @service_method(read_only=True, since="1.0")
    def titles(self, *, actor, page_size: int = 25, cursor: str | None = None) -> Page[str]:
        from examples.notes.models import Note

        queryset = authorized_queryset(Note, user=actor, action="read").order_by("code")
        return paginate(queryset, convert=lambda note: note.title, page_size=page_size, cursor=cursor)

    @service_method(read_only=True, since="1.0", allow_system=True)
    def heading(self, *, actor) -> str:
        return str(application_state("example_notes")["configuration"].get("heading", ""))


@pytest.fixture
def records(skeleton_db):
    assert connection.vendor == "postgresql"
    return build_security_fixture()


def all_titles(actor):
    titles, cursor = [], None
    while True:
        page = NoteTitles().titles(actor=actor, page_size=1, cursor=cursor)
        titles.extend(page.items)
        if page.next_cursor is None:
            return titles
        cursor = page.next_cursor


def test_service_reads_only_what_the_actor_reads_in_its_tenant(records):
    with actor_tenant(records.alice, records.a):
        titles = all_titles(records.alice)
        assert "Private" in titles
        assert "Foreign" not in titles
    with actor_tenant(records.bob, records.a):
        assert "Private" not in all_titles(records.bob)
    # Alice is a member of B without TablePermission: default deny, nothing from A leaks.
    with actor_tenant(records.alice, records.b):
        with pytest.raises(Forbidden):
            NoteTitles().titles(actor=records.alice)


def test_service_refuses_another_actor_than_the_context(records):
    with actor_tenant(records.alice, records.a):
        with pytest.raises(Forbidden):
            NoteTitles().titles(actor=records.bob)
        with pytest.raises(Forbidden):
            NoteTitles().titles(actor=None)


def test_system_job_calls_only_allow_system_methods(records):
    with system_context(reason="test:service-api-system", organization_id=records.a.pk):
        assert isinstance(NoteTitles().heading(actor=None), str)
        with pytest.raises(Forbidden):
            NoteTitles().titles(actor=None)


def test_disabled_provider_is_unavailable_only_in_that_tenant(records):
    with actor_tenant(records.bob, records.b):
        configure_application(
            "example_notes",
            user=records.bob,
            enabled=False,
            configuration={},
            revision=activation_revision("example_notes"),
        )
        with pytest.raises(Unavailable):
            NoteTitles().titles(actor=records.bob)
    with actor_tenant(records.alice, records.a):
        assert "Private" in all_titles(records.alice)
