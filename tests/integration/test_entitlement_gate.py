"""The core entitlement hooks (#395): a registered policy closes apps, limits access, stops app jobs and sets the quota.

Runs on PostgreSQL with the runtime role, with a fake policy registered for each test.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from examples.notes.models import Note
from examples.notes.service_api import NoteService
from skeleton.common import entitlements
from skeleton.common.entitlements import NO_OPINION, AccessLevel, override_policy
from skeleton.jobs.models import Job
from skeleton.jobs.periodic import register_periodic, schedule_due
from skeleton.jobs.registry import JobType, register_job_type
from skeleton.service_api.errors import Unavailable
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_job_types
from skeleton.testing.service_api import build_service_scenario
from tests import support

pytestmark = pytest.mark.needs_example_app
NOW = datetime(2026, 11, 9, 10, 7, 30, tzinfo=UTC)
CALLS = []


class FakePolicy:
    """Per organization: unpaid apps, access level, storage allowance and who may settle."""

    def __init__(self):
        self.unpaid = {}
        self.levels = {}
        self.quotas = {}
        self.settlers = set()

    def entitled_apps(self, organization_id, app_labels):
        return {label: label not in self.unpaid.get(organization_id, ()) for label in app_labels}

    def organization_access(self, organization_id):
        return self.levels.get(organization_id, AccessLevel.ACTIVE)

    def storage_quota(self, organization_id):
        return self.quotas.get(organization_id, NO_OPINION)

    def may_use_exempt_routes(self, organization_id, user):
        return user.pk in self.settlers


def handler(**kwargs):
    from skeleton.tenancy.context import current_context

    CALLS.append(current_context().organization_id)


@pytest.fixture
def gate(skeleton_db, monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(entitlements, "_exempt_segments", {"settle"})
    policy = FakePolicy()
    with isolated_job_types(keep_existing=True):
        register_job_type(JobType("test.notes_job", handler, {}, owner_app="example_notes"))
        register_job_type(JobType("test.core_job", handler, {}))
        register_periodic("gate-notes", "test.notes_job", every=timedelta(minutes=5))
        scenario = build_service_scenario(Note, slug="entitlement")
        with override_policy(policy):
            yield scenario, policy


def login(scenario, user):
    return support.login(user, password=scenario.password)


def home(organization):
    return reverse("example_notes:home", kwargs={"organization_id": organization.pk})


def summary(organization):
    return reverse("example_notes_api:summary", kwargs={"organization_id": organization.pk})


def org_page(organization):
    return reverse("web:dashboard", kwargs={"organization_id": organization.pk})


def test_without_a_policy_everything_is_allowed(skeleton_db):
    with override_policy(None):
        organization_id = object()
        assert entitlements.entitled_apps(organization_id, ["a", "b"]) == {"a": True, "b": True}
        assert entitlements.organization_access(organization_id) is AccessLevel.ACTIVE
        assert entitlements.storage_quota(organization_id) is NO_OPINION
        assert entitlements.may_use_exempt_routes(organization_id, None) is True


def test_registering_a_second_policy_is_refused():
    with override_policy(None):
        first, second = FakePolicy(), FakePolicy()
        entitlements.register_policy(first)
        entitlements.register_policy(first)  # Idempotent for the same policy.
        with pytest.raises(ValueError):
            entitlements.register_policy(second)
    with pytest.raises(ValueError):
        entitlements.register_exempt_segment("a/b")


def test_an_unpaid_app_closes_everywhere_and_reopens_with_its_configuration(gate):
    from skeleton.system.applications import application_state, require_application_enabled

    scenario, policy = gate
    note = scenario.create(scenario.a, code="G-1", title="Gate")
    client = login(scenario, scenario.member)
    assert client.get(home(scenario.a)).status_code == 200
    policy.unpaid[scenario.a.pk] = {"example_notes"}
    assert client.get(home(scenario.a)).status_code == 404
    assert client.get(summary(scenario.a)).status_code == 404
    assert client.get(home(scenario.b)).status_code == 200  # Organization B paid.
    with scenario.as_member() as actor:
        state = application_state("example_notes")
        assert (state["available"], state["enabled"]) == (False, True)  # Activation is kept.
        with pytest.raises(PermissionDenied):
            require_application_enabled("example_notes")
        with pytest.raises(Unavailable):
            NoteService().get_note(actor=actor, note_id=note.pk)
    policy.unpaid.clear()
    assert client.get(home(scenario.a)).status_code == 200


def test_activating_an_unpaid_app_is_refused_with_its_own_code(gate):
    from skeleton.common.exceptions import ApplicationNotEntitled
    from skeleton.system.applications import application_state, configure_application

    scenario, policy = gate
    with scenario.acting(scenario.admin_a) as admin:
        state = application_state("example_notes")
        configure_application(
            "example_notes", user=admin, enabled=False, configuration=state["configuration"], revision=state["revision"]
        )
    policy.unpaid[scenario.a.pk] = {"example_notes"}
    with scenario.acting(scenario.admin_a) as admin:
        state = application_state("example_notes")
        with pytest.raises(ApplicationNotEntitled) as refused:
            configure_application(
                "example_notes",
                user=admin,
                enabled=True,
                configuration=state["configuration"],
                revision=state["revision"],
            )
    assert refused.value.error_dict["enabled"][0].code == "app_not_entitled"


def test_restricted_organization_reads_but_does_not_write(gate):
    from skeleton.common.exceptions import OrganizationAccessLimited
    from skeleton.records.services import create_record
    from skeleton.system.registry import get_spec

    scenario, policy = gate
    note = scenario.create(scenario.a, code="G-2", title="Gate")
    policy.levels[scenario.a.pk] = AccessLevel.RESTRICTED
    client = login(scenario, scenario.member)
    assert client.get(home(scenario.a)).status_code == 200
    assert client.get(summary(scenario.a)).status_code == 200
    csrf = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    refused = client.post(
        reverse("api:records", kwargs={"organization_id": scenario.a.pk, "table_key": "example_notes.note"}),
        {"code": "G-3", "title": "Refused"},
        content_type="application/json",
        **csrf,
    )
    assert (refused.status_code, refused.json()["code"]) == (403, "organization_restricted")
    with scenario.as_member() as actor:
        assert NoteService().get_note(actor=actor, note_id=note.pk).code == "G-2"  # Reads stay.
        with pytest.raises(Unavailable) as command:
            NoteService().create_note(actor=actor, code="G-4", title="Refused")
        assert command.value.code == "organization_restricted"
        with pytest.raises(OrganizationAccessLimited):
            create_record(get_spec("example_notes.note"), user=actor, data={"code": "G-5", "title": "Refused"})
    with system_context(reason="test:restricted-infrastructure", organization_id=scenario.a.pk):
        Note(organization=scenario.a, owner=scenario.member, code="G-6", title="Job").save()  # Jobs keep writing.


def test_suspended_organization_shows_the_page_and_only_exempt_routes_pass(gate, rf):
    from skeleton.tenancy.middleware import limited_access_response

    scenario, policy = gate
    policy.levels[scenario.a.pk] = AccessLevel.SUSPENDED
    client = login(scenario, scenario.member)
    page = client.get(org_page(scenario.a))
    assert page.status_code == 403 and "suspenso" in page.content.decode()
    api = client.get(summary(scenario.a))
    assert (api.status_code, api.json()["code"]) == (403, "organization_suspended")
    assert login(scenario, scenario.outsider).get(org_page(scenario.a)).status_code == 404  # Not revealed.
    assert client.get(org_page(scenario.b)).status_code == 200
    exempt = f"/o/{scenario.a.pk}/settle/"
    request = rf.post(exempt)
    assert limited_access_response(request, scenario.a.pk, scenario.member).status_code == 403
    policy.settlers.add(scenario.member.pk)
    assert limited_access_response(request, scenario.a.pk, scenario.member) is None
    assert (
        limited_access_response(rf.get(f"/api/v1/o/{scenario.a.pk}/settle/x/"), scenario.a.pk, scenario.member) is None
    )
    assert limited_access_response(rf.get(org_page(scenario.a)), scenario.a.pk, scenario.member).status_code == 403
    with scenario.as_member() as actor:
        with pytest.raises(Unavailable) as read:
            NoteService().note_count(actor=actor)
        assert read.value.code == "organization_suspended"


def test_suspension_cancels_app_jobs_but_not_core_jobs(gate):
    from skeleton.jobs.services import enqueue

    scenario, policy = gate
    for organization in (scenario.a, scenario.b):
        with system_context(reason="test:gate-enqueue", organization_id=organization.pk):
            enqueue("test.notes_job", organization_id=organization.pk, payload={})
            enqueue("test.core_job", organization_id=organization.pk, payload={})
    policy.levels[scenario.a.pk] = AccessLevel.SUSPENDED
    run_pending_jobs()
    with system_context(reason="test:gate-jobs", organization_id=scenario.a.pk):
        states = dict(Job.objects.filter(job_organization_id=scenario.a.pk).values_list("job_type", "last_error_code"))
    assert states["test.notes_job"] == "organization_suspended"
    assert states["test.core_job"] == ""
    assert sorted(CALLS) == sorted([scenario.a.pk, scenario.b.pk, scenario.b.pk])
    schedule_due(now=NOW)
    with system_context(reason="test:gate-periodic", organization_id=scenario.a.pk):
        assert not Job.objects.filter(
            job_type="test.notes_job", job_organization_id=scenario.a.pk, idempotency_key__startswith="periodic:"
        ).exists()
    with system_context(reason="test:gate-periodic-b", organization_id=scenario.b.pk):
        assert Job.objects.filter(
            job_type="test.notes_job", job_organization_id=scenario.b.pk, idempotency_key__startswith="periodic:"
        ).exists()


def test_the_subscription_allowance_sets_the_quota_below_the_platform_override(gate):
    from skeleton.files.models import StorageQuota
    from skeleton.files.quota import quota_for

    scenario, policy = gate
    with system_context(reason="test:gate-quota", organization_id=scenario.a.pk):
        assert quota_for(scenario.a.pk) == (None, False)
        policy.quotas[scenario.a.pk] = 150
        assert quota_for(scenario.a.pk) == (150, False)
        policy.quotas[scenario.a.pk] = None
        assert quota_for(scenario.a.pk) == (None, False)  # The plan may be unlimited.
    with system_context(reason="test:gate-override"):
        StorageQuota(quota_organization=scenario.a, quota_bytes=10).save()
    with system_context(reason="test:gate-quota-override", organization_id=scenario.a.pk):
        assert quota_for(scenario.a.pk) == (10, True)
