"""Bundles of apps as tiers (#714) and the self-service signup of an organization (#715).

PostgreSQL with the runtime role. A bundle activates its apps and the member limit follows;
changing it withdraws the apps it does not keep; the signup creates the person's organization
with a free bundle, applies the policy, and refuses everything that is not allowed.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.system import app_registry, availability, bundles
from skeleton.tenancy import signup
from skeleton.tenancy.context import request_context, system_context, tenant_context

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults, pytest.mark.needs_example_app]
PASSWORD = "Bundles-signup-Password!714"
NOTES = "example_notes"
BASE = "example_base"


@pytest.fixture(autouse=True)
def clean_rate_limit():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def setup(monkeypatch, settings):
    notes = app_registry.get_app(NOTES)
    base = replace(
        notes, app_label=BASE, app_id="org.example.base-stand-in", label="Base", dependencies=(), canonical=None
    )
    monkeypatch.setitem(app_registry._manifests, BASE, base)
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    platform, org_a = bootstrap_identity(
        email="bundles-platform@example.test", organization="bundles-a", name="A", password=PASSWORD
    )
    owner_b, org_b = bootstrap_identity(
        email="bundles-b@example.test", organization="bundles-b", name="B", password=PASSWORD
    )
    with system_context(reason="test:bundles-demote"):
        owner_b.is_staff = owner_b.is_superuser = False
        owner_b.save(update_fields=["is_staff", "is_superuser"])
    for label, limits in ((BASE, {"members": 2}), (NOTES, {"members": 7})):
        as_platform(
            platform,
            lambda label=label: availability.set_app_availability(label, user=platform, mode="selected", revision=0),
        )
        as_platform(
            platform,
            lambda label=label, limits=limits: availability.set_app_limits(
                label, user=platform, limits=limits, revision=1
            ),
        )
    basic = save(
        platform, {"code": "basico", "label": "Básico", "apps": [BASE], "signup_visible": True, "is_default": True}
    )
    pro = save(platform, {"code": "pro", "label": "Pro", "apps": [BASE, NOTES], "position": 1, "signup_visible": True})
    return platform, org_a, owner_b, org_b, basic, pro


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:bundles"):
        return fn()


def save(platform, data, **kwargs):
    return as_platform(platform, lambda: bundles.save_bundle(actor=platform, data=data, **kwargs))


def effective(organization):
    from skeleton.system.applications import application_states

    with system_context(reason="test:bundles-states", organization_id=organization.pk):
        return {label for label, state in application_states().items() if state["effective"]}


def member_limit(organization):
    from skeleton.system.limits import limit_status

    with system_context(reason="test:bundles-limit", organization_id=organization.pk):
        return limit_status("members").limit


def test_bundle_validation(setup):
    platform, *_ = setup
    for data in (
        {"code": "x", "label": "X", "apps": ["not_installed"]},
        {"code": "y", "label": "Y", "apps": [BASE, BASE]},
        {"code": "basico", "label": "Again", "apps": []},
        {"code": "z", "label": "Z", "apps": [], "is_default": True},  # A second default.
    ):
        with pytest.raises(ValidationError):
            save(platform, data)


def test_assign_and_change_bundle(setup):
    from skeleton.system.models import OrganizationBundle

    platform, org_a, owner_b, org_b, basic, pro = setup
    preview = as_platform(
        platform, lambda: bundles.bundle_change_preview(actor=platform, organization_id=org_a.pk, bundle=pro)
    )
    assert (preview.members_before, preview.members_after, set(preview.entering)) == (1, 10, {BASE, NOTES})
    as_platform(platform, lambda: bundles.assign_bundle(actor=platform, organization_id=org_a.pk, bundle=pro))
    assert {BASE, NOTES} <= effective(org_a) and member_limit(org_a) == 10
    assert effective(org_b) & {BASE, NOTES} == set() and member_limit(org_b) == 1  # B untouched.
    down = as_platform(
        platform, lambda: bundles.bundle_change_preview(actor=platform, organization_id=org_a.pk, bundle=basic)
    )
    assert (down.members_after, down.leaving) == (3, (NOTES,))
    as_platform(platform, lambda: bundles.assign_bundle(actor=platform, organization_id=org_a.pk, bundle=basic))
    assert BASE in effective(org_a) and NOTES not in effective(org_a) and member_limit(org_a) == 3
    with system_context(reason="test:bundles-row"):
        assert OrganizationBundle.objects.get(bundled_organization=org_a).bundle_id == basic.pk
    with request_context(actor=owner_b, request_id=uuid4(), source="test:bundles-denied"):
        with pytest.raises(PermissionDenied):
            bundles.assign_bundle(actor=owner_b, organization_id=org_b.pk, bundle=pro)


def test_lock_apps_keeps_them_on(setup):
    from skeleton.service_api.applications import ApplicationAdministrationService
    from skeleton.service_api.errors import Conflict
    from skeleton.testing.service_api import acting_as

    platform, org_a, *_rest = setup
    basic = _rest[2]
    as_platform(
        platform, lambda: bundles.assign_bundle(actor=platform, organization_id=org_a.pk, bundle=basic, lock_apps=True)
    )
    with acting_as(platform, org_a) as actor:
        state = ApplicationAdministrationService().application(actor=actor, app_label=BASE)
        with pytest.raises(Conflict) as locked:
            ApplicationAdministrationService().configure(
                actor=actor, app_label=BASE, enabled=False, configuration=(), expected_revision=state.revision
            )
    assert locked.value.code == "setting_locked"


def person(email, *, verified=True):
    from allauth.account.models import EmailAddress

    with system_context(reason="test:bundles-person"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=email, verified=verified, primary=True)
    return user


def do_signup(user, slug, bundle_code="basico"):
    with request_context(actor=user, request_id=uuid4(), source="test:signup"):
        return signup.signup_organization(user=user, name=slug.title(), slug=slug, bundle_code=bundle_code)


def test_signup_creates_the_organization_with_the_bundle(setup, settings):
    from skeleton.tenancy.models import Membership, Organization, OrganizationSignup

    platform, *_ = setup
    ana = person("ana@signup.example.test")
    with pytest.raises(PermissionDenied):
        do_signup(ana, "signup-off")  # Off by default.
    settings.SKELETON_ORGANIZATION_SIGNUP_ENABLED = True
    result = do_signup(ana, "signup-ana")
    organization = result.organization
    assert result.invoice_id is None
    assert BASE in effective(organization) and member_limit(organization) == 3
    with tenant_context(organization_id=organization.pk, actor=ana, source="test:signup-check"):
        assert Membership.objects.filter(user=ana, is_active=True).exists()
    with system_context(reason="test:signup-row"):
        assert OrganizationSignup.objects.get(signup_organization=organization).bundle_code == "basico"
    with pytest.raises(ValidationError):  # The ceiling of one organization per person.
        do_signup(ana, "signup-ana-2")
    with system_context(reason="test:signup-none"):
        assert not Organization.objects.filter(slug="signup-ana-2").exists()


def test_signup_refusals(setup, settings):
    from skeleton.tenancy.models import Organization
    from skeleton.tenancy.provisioning import save_rule

    platform, org_a, *_ = setup
    settings.SKELETON_ORGANIZATION_SIGNUP_ENABLED = True
    with pytest.raises(PermissionDenied):
        do_signup(person("unverified@signup.example.test", verified=False), "signup-unverified")
    bia = person("bia@signup.example.test")
    for slug, code in (("signup-bia", "hidden"), ("Bad Slug", "basico"), (org_a.slug, "basico")):
        with pytest.raises(ValidationError):
            do_signup(bia, slug, code)
    as_platform(
        platform,
        lambda: save_rule(actor=platform, key="organization.display_name", mode="required", value=None, locked=False),
    )
    assert signup.unavailable_reason() is not None
    with pytest.raises(ValidationError):
        do_signup(bia, "signup-bia")
    with system_context(reason="test:signup-none"):
        assert not Organization.objects.filter(slug__startswith="signup-bia").exists()


def test_signup_reason_is_only_for_an_authenticated_person():
    from skeleton.tenancy.context import SIGNUP_REASONS, InvalidContext

    assert SIGNUP_REASONS == {"organization:signup"}
    with request_context(actor=None, request_id=uuid4(), source="test:signup-anonymous"):
        with pytest.raises(InvalidContext), system_context(reason="organization:signup"):
            pass


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def test_pages(setup, settings):
    platform, org_a, *_rest = setup
    pro = _rest[3]
    admin = login(platform)
    assert admin.get(reverse("platform:bundles")).status_code == 200
    preview = admin.get(reverse("platform:organization-bundle", args=[org_a.pk]), {"bundle": "pro"})
    assert preview.status_code == 200 and preview.context["preview"].members_after == 10
    applied = admin.post(
        reverse("platform:organization-bundle", args=[org_a.pk]),
        {"bundle": "pro", "csrfmiddlewaretoken": admin.cookies["csrftoken"].value},
        follow=True,
    )
    assert "Limite de membros: 1 → 10" in applied.content.decode()
    carla = person("carla@signup.example.test")
    client = login(carla)
    assert client.get(reverse("web:organization-signup")).status_code == 404  # Off.
    settings.SKELETON_ORGANIZATION_SIGNUP_ENABLED = True
    page = client.get(reverse("web:organization-signup"))
    assert page.status_code == 200 and "Inclui até 3 membros" in page.content.decode()
    assert "web:organization-signup" not in page.content.decode()
    created = client.post(
        reverse("web:organization-signup"),
        {
            "name": "Carla",
            "slug": "signup-carla",
            "bundle": "basico",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert created.status_code == 302
    listing = admin.get(reverse("platform:organizations"), {"signup": "1"})
    assert [row.slug for row in listing.context["rows"]] == ["signup-carla"]
    assert pro.code == "pro"
