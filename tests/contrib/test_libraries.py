"""First-party libraries (#792): the calendar follows the apps that use it.

PostgreSQL with the runtime role. A library has no availability nor activation of its own: it is
effective in an organization while an effective app there depends on it, never in another
organization, and every write path that keeps a row per app refuses it with ``code="library"``.
Collected with SKELETON_BUNDLED_APPS=calendar,ledger (CI jobs "apps-*").
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.system import app_registry, applications, availability, bundles
from skeleton.tenancy.context import request_context, system_context

pytestmark = [
    pytest.mark.skipif(
        not (apps.is_installed("skeleton.contrib.calendar") and apps.is_installed("skeleton.contrib.ledger")),
        reason="SKELETON_BUNDLED_APPS=calendar,ledger is required",
    ),
    pytest.mark.usefixtures("skeleton_db"),
    pytest.mark.closed_app_defaults,
    pytest.mark.needs_example_app,
]
PASSWORD = "Libraries-fixture-Password!792"
CALENDAR, LEDGER = "skeleton_calendar", "skeleton_ledger"
USER = "example_calendar_user"


def demote(user):
    with system_context(reason="test:libraries-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup(monkeypatch):
    """An app that depends on the calendar (a stand-in of the notes example), available as ``selected``."""
    notes = app_registry.get_app("example_notes")
    user_app = replace(
        notes,
        app_label=USER,
        app_id="org.example.calendar-user",
        label="Usa a agenda",
        dependencies=(CALENDAR,),
        dependency_versions=(),
        canonical=None,
        navigation=(),
    )
    monkeypatch.setitem(app_registry._manifests, USER, user_app)
    platform, org_a = bootstrap_identity(
        email="libraries-platform@example.test", organization="libraries-a", name="A", password=PASSWORD
    )
    admin_b, org_b = bootstrap_identity(
        email="libraries-b@example.test", organization="libraries-b", name="B", password=PASSWORD
    )
    demote(admin_b)
    admin_b.refresh_from_db()
    as_platform(platform, lambda: availability.set_app_availability(USER, user=platform, mode="selected", revision=0))
    return platform, org_a, admin_b, org_b


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:libraries"):
        return fn()


def states(organization):
    with system_context(reason="test:libraries-states", organization_id=organization.pk):
        return applications.application_states()


def usable(organization, label):
    from skeleton.common.exceptions import ApplicationUnavailable

    with system_context(reason="test:libraries-gate", organization_id=organization.pk):
        try:
            applications.require_application_enabled(label)
        except ApplicationUnavailable:
            return False
    return True


def code_of(refused):
    return {error.code for errors in refused.value.error_dict.values() for error in errors}


def test_the_library_follows_the_app_that_uses_it(setup):
    from skeleton.system.models import AppAvailability, AppInstallation

    platform, org_a, _, org_b = setup
    assert not states(org_a)[CALENDAR]["effective"] and not usable(org_a, CALENDAR)
    # The platform assigns only the app: no availability nor activation of the calendar is needed.
    as_platform(platform, lambda: applications.assign_app(USER, user=platform, organization_id=org_a.pk))
    in_a, in_b = states(org_a), states(org_b)
    assert in_a[USER]["effective"] and in_a[CALENDAR]["effective"] and in_a[CALENDAR]["used_by"] == [USER]
    assert in_a[CALENDAR]["library"] and not in_a[LEDGER]["effective"]  # Nothing in A uses the ledger.
    assert usable(org_a, CALENDAR) and not usable(org_b, CALENDAR)  # B never sees A's app (A/B).
    assert not in_b[USER]["effective"] and not in_b[CALENDAR]["effective"]
    with system_context(reason="test:libraries-rows"):
        assert not AppAvailability.all_objects.filter(app_label=CALENDAR).exists()
    with system_context(reason="test:libraries-rows", organization_id=org_a.pk):
        assert not AppInstallation.all_objects.filter(app_label=CALENDAR).exists()
    # Withdrawing the last app that uses it closes the library; nothing of its own was written.
    as_platform(platform, lambda: applications.unassign_app(USER, user=platform, organization_id=org_a.pk))
    assert not states(org_a)[CALENDAR]["effective"] and not usable(org_a, CALENDAR)


def test_a_library_has_no_gate_of_its_own(setup):
    from skeleton.service_api.applications import ApplicationAdministrationService
    from skeleton.service_api.errors import NotFound

    platform, org_a, admin_b, org_b = setup
    for refuse in (
        lambda: availability.set_app_availability(CALENDAR, user=platform, mode="all", revision=0),
        lambda: availability.grant_app(CALENDAR, user=platform, organization_id=org_a.pk),
        lambda: availability.set_app_limits(CALENDAR, user=platform, limits={"members": 1}, revision=0),
        lambda: availability.availability_summary(CALENDAR, user=platform),
        lambda: applications.assign_app(CALENDAR, user=platform, organization_id=org_a.pk),
        lambda: applications.configure_application_by_platform(
            CALENDAR, user=platform, organization_id=org_a.pk, values={}, revision=0
        ),
    ):
        with pytest.raises(ValidationError) as refused:
            as_platform(platform, refuse)
        assert code_of(refused) == {"library"}
    from skeleton.testing.service_api import acting_as

    with acting_as(admin_b, org_b):
        with pytest.raises(ValidationError) as refused:
            applications.configure_application(CALENDAR, user=admin_b, enabled=True, configuration={}, revision=0)
        assert code_of(refused) == {"library"}
        service = ApplicationAdministrationService()
        assert CALENDAR not in {state.app_label for state in service.applications(actor=admin_b)}
        with pytest.raises(NotFound):
            service.application(actor=admin_b, app_label=CALENDAR)
    with pytest.raises(CommandError, match="library"):
        call_command("set_app_availability", CALENDAR, "--mode", "all", verbosity=0)


def test_bundles_and_billing_take_apps_only(setup):
    from skeleton.billing.models import Price, Product
    from skeleton.system.models import AppBundle

    platform, org_a, _, _ = setup
    with pytest.raises(ValidationError) as refused:
        as_platform(
            platform, lambda: bundles.save_bundle(actor=platform, data={"code": "x", "label": "X", "apps": [CALENDAR]})
        )
    assert code_of(refused) == {"library"}
    bundle = as_platform(
        platform, lambda: bundles.save_bundle(actor=platform, data={"code": "uses", "label": "U", "apps": [USER]})
    )
    with system_context(reason="test:libraries-legacy-bundle"):
        # A bundle saved before #792 may still list the library: it is ignored, never an error.
        AppBundle.all_objects.filter(pk=bundle.pk).update(apps=[CALENDAR, USER])
        bundle.refresh_from_db()
    assert bundles.bundle_apps(bundle) == [USER]
    as_platform(platform, lambda: bundles.assign_bundle(actor=platform, organization_id=org_a.pk, bundle=bundle))
    assert states(org_a)[CALENDAR]["effective"]
    with pytest.raises(ValidationError) as refused:
        Product(code="agenda", name="Agenda", kind=Product.Kind.APP, app_label=CALENDAR).clean()
    assert code_of(refused) == {"library"}
    with pytest.raises(ValidationError) as refused:
        Price(currency="BRL", included_apps=[USER, CALENDAR]).clean()
    assert "library" in code_of(refused)


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def test_the_platform_shows_libraries_read_only(setup):
    platform, *_ = setup
    client = login(platform)
    listing = client.get(reverse("platform:apps"))
    assert listing.status_code == 200
    assert [item["app_label"] for item in listing.context["libraries"]] == [CALENDAR, LEDGER]
    assert CALENDAR not in {item["app_label"] for item in listing.context["applications"]}
    calendar = next(item for item in listing.context["libraries"] if item["app_label"] == CALENDAR)
    assert [item["app_label"] for item in calendar["used_by"]] == [USER]
    detail = client.get(reverse("platform:app", kwargs={"app_label": CALENDAR}))
    assert detail.status_code == 200 and detail.context["library"] and detail.context["availability"] is None
    assert client.get(reverse("platform:app-availability", kwargs={"app_label": CALENDAR})).status_code == 404
    for name in ("platform:app-limits", "platform:app-grant"):
        response = client.post(
            reverse(name, kwargs={"app_label": CALENDAR}), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value}
        )
        assert response.status_code == 404
    from skeleton.web.platform.bundles_views import BundleForm

    choices = dict(BundleForm().fields["apps"].choices)
    assert USER in choices and CALENDAR not in choices and LEDGER not in choices
