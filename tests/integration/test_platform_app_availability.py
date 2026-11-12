"""Platform screens of app availability and the organization's apps card (#23)."""

from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.system import availability
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.applications import activate_app
from skeleton.web.platform.forms import AppAvailabilityForm, AppGrantForm, AppRevokeForm

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults, pytest.mark.needs_example_app]
PASSWORD = "Availability-screens-Password!23"
NOTES = "example_notes"


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def post(client, name, data):
    return client.post(
        reverse(name, kwargs={"app_label": NOTES}), {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    )


def demote(user):
    with system_context(reason="test:availability-screens-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup():
    platform, org_a = bootstrap_identity(
        email="screens-platform@example.test", organization="screens-a", name="Screens A", password=PASSWORD
    )
    admin_b, org_b = bootstrap_identity(
        email="screens-admin-b@example.test", organization="screens-b", name="Screens B", password=PASSWORD
    )
    demote(admin_b)
    admin_b.refresh_from_db()
    return platform, org_a, admin_b, org_b, login(platform)


def available(organization):
    return availability.app_available(NOTES, organization_id=organization.pk)


def audit_count(platform, *organizations):
    from skeleton.audit.models import AuditEvent

    with request_context(actor=platform, request_id=uuid4(), source="test:availability-screens-audit"):
        total = AuditEvent.objects.filter(organization_id__isnull=True).count()
    for organization in organizations:
        with system_context(reason="test:availability-screens-audit", organization_id=organization.pk):
            total += AuditEvent.objects.count()
    return total


def test_forms_validate_mode_organization_and_revision(setup):
    _, org_a, _, org_b, _ = setup
    assert not AppAvailabilityForm({"mode": "everyone", "expected_revision": 0}).is_valid()
    assert not AppAvailabilityForm({"mode": "all", "expected_revision": -1}).is_valid()
    assert AppAvailabilityForm({"mode": "selected", "expected_revision": 0}).is_valid()
    assert not AppGrantForm({"organization": str(uuid4())}).is_valid()
    assert not AppGrantForm({"organization": str(org_a.pk)}, granted_ids={org_a.pk}).is_valid()
    assert AppGrantForm({"organization": str(org_b.pk)}, granted_ids={org_a.pk}).is_valid()
    assert not AppRevokeForm({"organization": str(org_a.pk), "expected_revision": 0}).is_valid()
    assert not AppRevokeForm({"organization": "not-a-uuid", "expected_revision": 1}).is_valid()


def test_grant_picker_offers_only_active_organizations(setup):
    from skeleton.accounts.services import update_organization

    platform, org_a, _, org_b, client = setup
    with request_context(actor=platform, request_id=uuid4(), source="test:availability-screens-inactive"):
        update_organization(org_b, data={"name": org_b.name, "is_active": False}, expected_revision=org_b.revision)
    page = client.get(reverse("platform:app-availability", kwargs={"app_label": NOTES}))
    assert page.status_code == 200
    offered = set(page.context["grant_form"].fields["organization"].queryset.values_list("pk", flat=True))
    assert org_a.pk in offered and org_b.pk not in offered
    assert post(client, "platform:app-grant", {"organization": str(org_b.pk)}).status_code == 400
    assert post(client, "platform:app-grant", {"organization": str(uuid4())}).status_code == 400
    assert not available(org_b)


def test_platform_sets_mode_grants_and_revokes_with_cas(setup):
    from skeleton.system.models import AppInstallation

    platform, org_a, admin_b, org_b, client = setup
    page = client.get(reverse("platform:app-availability", kwargs={"app_label": NOTES}))
    assert page.status_code == 200 and page.context["availability"].mode is None
    assert page.context["mode_form"]["expected_revision"].value() == 0

    assert post(client, "platform:app-availability", {"mode": "selected", "expected_revision": 0}).status_code == 302
    stale = post(client, "platform:app-availability", {"mode": "all", "expected_revision": 0})
    assert stale.status_code == 409
    assert not available(org_a) and not available(org_b)

    assert post(client, "platform:app-grant", {"organization": str(org_a.pk)}).status_code == 302
    assert available(org_a) and not available(org_b)
    activate_app(NOTES, org_a.pk)
    page = client.get(reverse("platform:app-availability", kwargs={"app_label": NOTES}))
    [grant] = page.context["availability"].grants
    assert grant.organization_id == org_a.pk and grant.blocked_by == ()
    # A grant already in the list cannot be offered again.
    assert post(client, "platform:app-grant", {"organization": str(org_a.pk)}).status_code == 400

    revoke = {"organization": str(org_a.pk), "expected_revision": grant.revision}
    assert post(client, "platform:app-revoke", {**revoke, "expected_revision": grant.revision + 1}).status_code == 409
    assert available(org_a)
    assert post(client, "platform:app-revoke", revoke).status_code == 302
    assert not available(org_a)
    # Revoked twice from the same (now stale) page: a conflict, not a silent success.
    assert post(client, "platform:app-revoke", revoke).status_code == 409
    with tenant_context(organization_id=org_a.pk, actor=platform, source="test:availability-screens-kept"):
        assert AppInstallation.objects.get(app_label=NOTES).enabled is True

    # Re-grant restores the same row; the organization's activation comes back with it.
    assert post(client, "platform:app-grant", {"organization": str(org_a.pk)}).status_code == 302
    assert available(org_a)
    home = reverse("example_notes:home", kwargs={"organization_id": org_a.pk})
    # bootstrap_identity made the platform actor A's administrator: the kept activation is back.
    assert client.get(home).status_code == 200

    mode = client.get(reverse("platform:app-availability", kwargs={"app_label": NOTES})).context["mode_form"]
    revision = mode["expected_revision"].value()
    assert post(client, "platform:app-availability", {"mode": "all", "expected_revision": revision}).status_code == 302
    assert available(org_b)


def test_screens_refuse_non_platform_users_csrf_and_stale_authentication(setup):
    platform, org_a, admin_b, org_b, client = setup
    outsider = login(admin_b)
    urls = [
        reverse("platform:app-availability", kwargs={"app_label": NOTES}),
        reverse("platform:app", kwargs={"app_label": NOTES}),
        reverse("platform:organization", kwargs={"sysid": org_b.pk}),
    ]
    for url in urls:
        assert outsider.get(url).status_code == 403
    for name in ("platform:app-availability", "platform:app-grant", "platform:app-revoke"):
        response = post(outsider, name, {"mode": "all", "expected_revision": 0, "organization": str(org_b.pk)})
        assert response.status_code == 403
    assert not available(org_b)

    url = reverse("platform:app-grant", kwargs={"app_label": NOTES})
    assert client.get(url).status_code == 405
    assert client.post(url, {"organization": str(org_a.pk)}).status_code == 403  # CSRF
    assert not available(org_a)

    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = post(client, "platform:app-grant", {"organization": str(org_a.pk)})
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    assert client.get(reverse("platform:app-availability", kwargs={"app_label": NOTES})).status_code == 200
    assert not available(org_a)


def test_opening_the_screens_writes_nothing(setup):
    platform, org_a, _, org_b, client = setup
    assert post(client, "platform:app-availability", {"mode": "selected", "expected_revision": 0}).status_code == 302
    assert post(client, "platform:app-grant", {"organization": str(org_a.pk)}).status_code == 302
    before = audit_count(platform, org_a, org_b)
    for url in (
        reverse("platform:apps"),
        reverse("platform:app", kwargs={"app_label": NOTES}),
        reverse("platform:app-availability", kwargs={"app_label": NOTES}),
        reverse("platform:organization", kwargs={"sysid": org_a.pk}),
        reverse("platform:organization", kwargs={"sysid": org_b.pk}),
    ):
        assert client.get(url).status_code == 200
    assert audit_count(platform, org_a, org_b) == before


def test_organization_card_shows_gates_without_configuration(setup):
    platform, org_a, _, org_b, client = setup
    assert post(client, "platform:app-availability", {"mode": "selected", "expected_revision": 0}).status_code == 302
    assert post(client, "platform:app-grant", {"organization": str(org_a.pk)}).status_code == 302
    activate_app(NOTES, org_a.pk)
    activate_app(NOTES, org_b.pk)  # B enabled it, but the platform never granted B.

    def card(organization):
        page = client.get(reverse("platform:organization", kwargs={"sysid": organization.pk}))
        assert page.status_code == 200
        [row] = [app for app in page.context["overview"].applications if app.app_label == NOTES]
        return row, page

    row, page = card(org_a)
    assert (row.available, row.enabled, row.effective) == (True, True, True)
    assert b"configuration" not in page.content.lower()
    row, _ = card(org_b)
    assert (row.available, row.enabled, row.effective) == (False, True, False)


def test_dependents_granted_to_an_organization_block_its_revoke_button(setup, monkeypatch):
    from skeleton.system.models import AppAvailability, AppOrganizationGrant

    platform, org_a, _, org_b, client = setup
    assert post(client, "platform:app-availability", {"mode": "selected", "expected_revision": 0}).status_code == 302
    assert post(client, "platform:app-grant", {"organization": str(org_a.pk)}).status_code == 302
    assert post(client, "platform:app-grant", {"organization": str(org_b.pk)}).status_code == 302
    monkeypatch.setattr(availability, "dependent_labels", lambda label: ["child_app"] if label == NOTES else [])
    with system_context(reason="test:availability-screens-child"):
        child = AppAvailability(app_label="child_app", mode="selected")
        child.save()
        AppOrganizationGrant(availability=child, granted_organization=org_a).save()
    summary = {grant.organization_id: grant for grant in availability_summary_as(platform).grants}
    assert summary[org_a.pk].blocked_by == ("child_app",)
    assert summary[org_b.pk].blocked_by == ()
    page = client.get(reverse("platform:app-availability", kwargs={"app_label": NOTES}))
    assert b"Bloqueada por child_app" in page.content


def availability_summary_as(platform):
    with request_context(actor=platform, request_id=uuid4(), source="test:availability-screens-summary"):
        return availability.availability_summary(NOTES, user=platform)
