"""The platform assigns and withdraws apps of one organization (#699).

PostgreSQL with the runtime role. Assigning offers the app (``selected`` mode) and activates it
with its dependencies, in the system context of that organization only; withdrawing deactivates
it (refused while a dependent is active) and revokes the grant, keeping the configuration.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.system import app_registry, availability
from skeleton.system.applications import assign_app, unassign_app
from skeleton.tenancy.context import request_context, system_context, tenant_context

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults, pytest.mark.needs_example_app]
PASSWORD = "App-assignment-Password!699"
NOTES = "example_notes"
BASE = "example_base"


def demote(user):
    with system_context(reason="test:assign-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup(monkeypatch):
    """Two organizations, and a stand-in base app the notes app requires."""
    notes = app_registry.get_app(NOTES)
    base = replace(notes, app_label=BASE, app_id="org.example.base-stand-in", label="Base", dependencies=())
    monkeypatch.setitem(app_registry._manifests, BASE, base)
    monkeypatch.setitem(app_registry._manifests, NOTES, replace(notes, dependencies=(BASE,)))
    platform, org_a = bootstrap_identity(
        email="assign-platform@example.test", organization="assign-a", name="A", password=PASSWORD
    )
    admin_b, org_b = bootstrap_identity(
        email="assign-b@example.test", organization="assign-b", name="B", password=PASSWORD
    )
    demote(admin_b)
    admin_b.refresh_from_db()
    return platform, org_a, admin_b, org_b


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:assign-platform"):
        return fn()


def offer(platform, label, mode="selected", limits=None):
    as_platform(platform, lambda: availability.set_app_availability(label, user=platform, mode=mode, revision=0))
    if limits:
        as_platform(platform, lambda: availability.set_app_limits(label, user=platform, limits=limits, revision=1))


def states(organization):
    from skeleton.system.applications import application_states

    with system_context(reason="test:assign-states", organization_id=organization.pk):
        return {label: (state["enabled"], state["effective"]) for label, state in application_states().items()}


def events(organization):
    from skeleton.events.models import DomainEvent

    with system_context(reason="test:assign-events", organization_id=organization.pk):
        return list(DomainEvent.objects.order_by("created_at").values_list("event_type", "data"))


def member_limit(organization):
    from skeleton.system.limits import limit_status

    with system_context(reason="test:assign-limit", organization_id=organization.pk):
        return limit_status("members").limit


def test_assign_offers_and_activates_with_dependencies(setup, settings):
    from skeleton.audit.models import AuditEvent

    platform, org_a, _, org_b = setup
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 3
    offer(platform, BASE)
    offer(platform, NOTES, limits={"members": 7})
    assert member_limit(org_a) == 3
    as_platform(platform, lambda: assign_app(NOTES, user=platform, organization_id=org_a.pk))
    assert states(org_a)[BASE] == (True, True) and states(org_a)[NOTES] == (True, True)
    assert states(org_b)[NOTES] == (False, False)  # A's assignment never reaches B.
    assert member_limit(org_a) == 10 and member_limit(org_b) == 3
    enabled = [data["app_label"] for kind, data in events(org_a) if kind == "app.enabled"]
    assert enabled == [BASE, NOTES]
    # Already effective: nothing changes, no repeated event.
    as_platform(platform, lambda: assign_app(NOTES, user=platform, organization_id=org_a.pk))
    assert [data["app_label"] for kind, data in events(org_a) if kind == "app.enabled"] == [BASE, NOTES]
    with system_context(reason="test:assign-audit", organization_id=org_a.pk):
        sources = set(
            AuditEvent.objects.filter(model_label="sk_system.appinstallation").values_list("source", "actor_sysid")
        )
    assert sources == {("system:platform:apps", platform.pk)}


def test_unassign_refuses_active_dependents_and_keeps_configuration(setup):
    from skeleton.system.models import AppInstallation

    platform, org_a, _, _ = setup
    offer(platform, BASE)
    offer(platform, NOTES)
    as_platform(platform, lambda: assign_app(NOTES, user=platform, organization_id=org_a.pk))
    with pytest.raises(ValidationError) as blocked:
        as_platform(platform, lambda: unassign_app(BASE, user=platform, organization_id=org_a.pk))
    assert app_registry.get_app(NOTES).label in " ".join(blocked.value.messages)
    assert states(org_a)[BASE] == (True, True)
    as_platform(platform, lambda: unassign_app(NOTES, user=platform, organization_id=org_a.pk))
    assert states(org_a)[NOTES] == (False, False)
    assert not availability.app_available(NOTES, organization_id=org_a.pk)  # The grant is gone too.
    with system_context(reason="test:assign-kept", organization_id=org_a.pk):
        assert AppInstallation.objects.filter(app_label=NOTES).exists()  # Configuration kept.


def test_assign_requires_availability_and_entitlement(setup, settings, monkeypatch):
    from skeleton.common import entitlements

    platform, org_a, _, _ = setup
    with pytest.raises(ValidationError) as missing:
        as_platform(platform, lambda: assign_app(NOTES, user=platform, organization_id=org_a.pk))
    assert missing.value.error_dict["app_label"][0].code == "no_availability"
    offer(platform, BASE, mode="all")
    offer(platform, NOTES, mode="all")
    monkeypatch.setattr(
        entitlements, "entitled_apps", lambda organization_id, labels: {label: label != NOTES for label in labels}
    )
    with pytest.raises(ValidationError) as unpaid:
        as_platform(platform, lambda: assign_app(NOTES, user=platform, organization_id=org_a.pk))
    assert "App paga" in " ".join(unpaid.value.messages)
    assert states(org_a)[BASE] == (False, False)  # Nothing written: the whole assignment rolled back.


def test_only_the_platform_administrator(setup):
    _, _, admin_b, org_b = setup
    with request_context(actor=admin_b, request_id=uuid4(), source="test:assign-denied"):
        with pytest.raises(PermissionDenied):
            assign_app(NOTES, user=admin_b, organization_id=org_b.pk)
    with tenant_context(organization_id=org_b.pk, actor=admin_b, source="test:assign-org-admin"):
        with pytest.raises(PermissionDenied):
            unassign_app(NOTES, user=admin_b, organization_id=org_b.pk)


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def test_organization_page_assigns_and_withdraws(setup, settings):
    platform, org_a, admin_b, _ = setup
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 3
    offer(platform, BASE)
    offer(platform, NOTES, limits={"members": 7})
    client = login(platform)
    page = client.get(reverse("platform:organization", kwargs={"sysid": org_a.pk}))
    assert page.status_code == 200 and page.context["member_limit"] == 3
    row = next(item for item in page.context["apps"] if item["app"].app_label == NOTES)
    assert row["members"] == 7 and row["offered"]

    def post(name, who=client, label=NOTES):
        url = reverse(name, kwargs={"sysid": org_a.pk, "app_label": label})
        return who.post(url, {"csrfmiddlewaretoken": who.cookies["csrftoken"].value}, follow=True)

    done = post("platform:organization-app-assign")
    assert done.status_code == 200
    assert "Limite de membros: 3 → 10" in done.content.decode()
    refused = post("platform:organization-app-unassign", label=BASE)
    assert refused.status_code == 409
    withdrawn = post("platform:organization-app-unassign")
    assert "Limite de membros: 10 → 3" in withdrawn.content.decode()
    other = login(admin_b)
    assert post("platform:organization-app-assign", who=other).status_code in (403, 404)
    assert states(org_a)[NOTES] == (False, False)
