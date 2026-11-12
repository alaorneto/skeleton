"""Audit filters on every surface (#184), PostgreSQL with the runtime role.

The filters narrow the authorized log only: A/B, default deny and the per-role visibility of
``authorized_audit_queryset`` hold through every filter, and a request_id of another
organization reveals nothing.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from skeleton.service_api.audit import AuditLogService
from skeleton.service_api.errors import InvalidInput
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


@pytest.fixture
def scenario():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="audit-filters")


def _write(organization, user, request_id, **values):
    """Create and edit one note by user in a request with a known request_id."""
    from examples.notes.models import Note
    from skeleton.tenancy.context import request_context, tenant_context

    with request_context(actor=user, request_id=request_id, source="test:audit-filters"):
        with tenant_context(organization_id=organization.pk, actor=user, source="test:audit-filters"):
            values.setdefault("visibility", "organization")
            note = Note(organization=organization, owner=user, **values)
            note.save()
            note.title = note.title + " (edited)"
            note.save(update_fields={"title"})
    return note


def _domain_event(organization, note, request_id):
    from skeleton.events.models import DomainEvent
    from skeleton.tenancy.context import system_context

    with system_context(reason="fixture:audit-domain-event", organization_id=organization.pk):
        return DomainEvent.objects.create(
            organization=organization,
            event_type="example_notes.note.updated",
            table_key=TABLE,
            record_sysid=note.pk,
            record_revision=2,
            request_id=request_id,
            source="test:audit-filters",
            data={"title": "never shown"},
        )


@pytest.fixture
def written(scenario):
    request_a, request_hidden, request_b = uuid4(), uuid4(), uuid4()
    visible = _write(scenario.a, scenario.member, request_a, code="F-1", title="Visible")
    hidden = _write(scenario.a, scenario.admin_a, request_hidden, code="F-2", title="Hidden", visibility="private")
    foreign = _write(scenario.b, scenario.admin_b, request_b, code="F-3", title="Foreign")
    return {
        "visible": (visible, request_a),
        "hidden": (hidden, request_hidden),
        "foreign": (foreign, request_b),
        "events": {
            "visible": _domain_event(scenario.a, visible, request_a),
            "hidden": _domain_event(scenario.a, hidden, request_hidden),
            "foreign": _domain_event(scenario.b, foreign, request_b),
        },
    }


def test_each_filter_narrows_the_service_log(scenario, written, subtests):
    visible, request_a = written["visible"]
    hidden, request_hidden = written["hidden"]
    service = AuditLogService()
    now = timezone.now()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        everything = service.events(actor=admin, model_label=TABLE)
        assert everything.count == 4
        cases = {
            "actor": ({"actor_id": scenario.member.pk}, {visible.pk}),
            "operation": ({"operation": "create"}, {visible.pk, hidden.pk}),
            "request_id": ({"request_id": request_hidden}, {hidden.pk}),
            "period": ({"occurred_from": now - timedelta(hours=1), "occurred_to": now + timedelta(hours=1)}, None),
            "empty period": ({"occurred_to": now - timedelta(days=1)}, set()),
        }
        for name, (filters, expected) in cases.items():
            with subtests.test(filter=name):
                log = service.events(actor=admin, model_label=TABLE, **filters)
                if expected is None:
                    assert log.count == everything.count
                else:
                    assert {event.record_sysid for event in log.items} == expected
                    assert log.count == len(log.items)
        update = service.events(actor=admin, operation="update", request_id=request_a)
        assert [(event.operation, event.record_sysid) for event in update.items] == [("update", visible.pk)]
        for bad in (
            {"operation": "drop"},
            {"actor_id": "nope"},
            {"request_id": 7},
            {"occurred_from": now.replace(tzinfo=None)},
            {"occurred_from": "2026-11-01"},
            {"occurred_from": now, "occurred_to": now - timedelta(seconds=1)},
        ):
            with subtests.test(invalid=sorted(bad)), pytest.raises(InvalidInput):
                service.events(actor=admin, **bad)


def test_filters_never_widen_visibility(scenario, written, subtests):
    visible, request_a = written["visible"]
    _, request_hidden = written["hidden"]
    foreign, request_b = written["foreign"]
    service = AuditLogService()
    with acting_as(scenario.member, scenario.a) as member:
        with subtests.test("member sees only readable records"):
            assert service.events(actor=member, request_id=request_hidden).count == 0
            assert service.events(actor=member, actor_id=scenario.admin_a.pk, model_label=TABLE).count == 0
            created = service.events(actor=member, operation="create", model_label=TABLE)
            assert {event.record_sysid for event in created.items} == {visible.pk}
        with subtests.test("request_id of another organization"):
            assert service.events(actor=member, request_id=request_b).count == 0
            assert service.domain_events(actor=member, request_id=request_b) == ()
        with subtests.test("domain events follow the visible audit events"):
            correlated = service.domain_events(actor=member, request_id=request_a)
            assert [event.sysid for event in correlated] == [written["events"]["visible"].pk]
            assert not hasattr(correlated[0], "data")
            assert service.domain_events(actor=member, request_id=request_hidden) == ()
    with acting_as(scenario.denied, scenario.a) as denied:
        with subtests.test("default deny"):
            assert service.events(actor=denied, request_id=request_a).count == 0
            assert service.domain_events(actor=denied, request_id=request_a) == ()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        with subtests.test("admin sees the hidden request, never B"):
            hidden_events = service.domain_events(actor=admin, request_id=request_hidden)
            assert [event.sysid for event in hidden_events] == [written["events"]["hidden"].pk]
            assert service.events(actor=admin, request_id=request_b).count == 0
            assert foreign.pk not in {event.record_sysid for event in service.events(actor=admin).items}


def _login(user):
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def test_rest_and_web_surfaces_apply_the_same_filters(scenario, written, subtests):
    visible, request_a = written["visible"]
    hidden, request_hidden = written["hidden"]
    _, request_b = written["foreign"]
    member, admin = _login(scenario.member), _login(scenario.admin_a)
    api = f"/api/v1/o/{scenario.a.pk}/audit/"
    web = f"/o/{scenario.a.pk}/audit/"
    with subtests.test("REST filters"):
        data = admin.get(api, {"request_id": str(request_a), "operation": "update"}).json()
        assert [row["record_sysid"] for row in data["results"]] == [str(visible.pk)]
        assert [row["sysid"] for row in data["domain_events"]] == [str(written["events"]["visible"].pk)]
        assert "never shown" not in str(data)
        today = timezone.localdate().isoformat()
        assert admin.get(api, {"from": today, "to": today, "model_label": TABLE}).json()["count"] == 4
        assert admin.get(api, {"actor": str(scenario.member.pk), "model_label": TABLE}).json()["count"] == 2
        assert admin.get(api).json()["domain_events"] == []
    with subtests.test("REST refuses invalid and unknown filters"):
        for params in (
            {"from": "ontem"},
            {"operation": "drop"},
            {"actor": "x"},
            {"tenant": "x"},
            {"from": "2026-02-01", "to": "2026-01-01"},
        ):
            response = admin.get(api, params)
            assert response.status_code == 400 and response.json()["code"] == "invalid_filter"
    with subtests.test("REST member and foreign request"):
        assert member.get(api, {"request_id": str(request_hidden)}).json()["count"] == 0
        foreign = member.get(api, {"request_id": str(request_b)}).json()
        assert foreign["count"] == 0 and foreign["domain_events"] == []
    with subtests.test("organization page"):
        page = admin.get(web, {"request_id": str(request_hidden)})
        assert page.status_code == 200
        assert str(hidden.pk).encode() in page.content and str(visible.pk).encode() not in page.content
        assert b"example_notes.note.updated" in page.content
        assert str(hidden.pk).encode() not in member.get(web, {"request_id": str(request_hidden)}).content
        assert admin.get(web, {"operation": "drop"}).status_code == 400
        assert admin.get(web, {"page_size": "5"}).status_code == 400


def test_platform_page_filters_global_events(scenario, subtests):
    from skeleton.accounts.services import bootstrap_identity

    platform, _ = bootstrap_identity(
        email="audit-filters-platform@example.test",
        organization="audit-filters-platform",
        name="Platform",
        password=FIXTURE_PASSWORD,
    )
    client = _login(platform)
    with subtests.test("filter by operation and table"):
        page = client.get("/platform/audit/", {"operation": "create", "model_label": "sk_tenancy.organization"})
        assert page.status_code == 200 and str(scenario.a.pk).encode() in page.content
        assert str(scenario.a.pk).encode() not in client.get("/platform/audit/", {"operation": "restore"}).content
    with subtests.test("invalid filter"):
        assert client.get("/platform/audit/", {"from": "x"}).status_code == 400
        assert client.get("/platform/audit/", {"tenant": "x"}).status_code == 400


def test_filter_queries_use_the_investigation_indexes(scenario, written, subtests):
    """With sequential scans disabled, the planner reaches each filter through its index."""
    from skeleton.access.policies import authorized_audit_queryset

    _, request_a = written["visible"]
    cases = {
        "sk_audit_org_actor": {"actor_sysid": scenario.member.pk},
        "sk_audit_org_request": {"request_id": request_a},
        "sk_audit_org_created": {"created_at__gte": timezone.now() - timedelta(days=1)},
    }
    with acting_as(scenario.admin_a, scenario.a) as admin:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
        for index, lookups in cases.items():
            with subtests.test(index=index):
                plan = authorized_audit_queryset(admin).filter(**lookups).order_by("-created_at").explain()
                assert index in plan
