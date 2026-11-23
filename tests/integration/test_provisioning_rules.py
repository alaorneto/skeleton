"""What every new organization receives (#703): default, locked and required rules.

PostgreSQL with the runtime role. Rules apply inside ``create_organization`` (a missing
required value undoes the creation); a ``default`` rule can be applied to the existing
organizations by a global job, skipping those where the key is already locked.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import request_context, system_context
from skeleton.tenancy.provisioning import enqueue_apply, remove_rule, rules, save_rule
from skeleton.tenancy.services import create_organization

pytestmark = [pytest.mark.usefixtures("skeleton_db")]
PASSWORD = "Provisioning-Password!703"
NOTES = "example_notes"


def demote(user):
    with system_context(reason="test:rules-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def platform():
    user, _ = bootstrap_identity(
        email="rules-platform@example.test", organization="rules-home", name="Home", password=PASSWORD
    )
    return user


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:rules-platform"):
        return fn()


def rule(user, key, mode="default", value=None, locked=False, reason="", revision=0):
    return as_platform(
        user,
        lambda: save_rule(
            actor=user, key=key, mode=mode, value=value, locked=locked, reason=reason, expected_revision=revision
        ),
    )


def new_organization(user, slug, settings=None):
    with system_context(reason="test:rules-owner"):
        owner = create_identity(email=f"{slug}@rules.example.test", password=PASSWORD)
    with request_context(actor=user, request_id=uuid4(), source="test:rules-create"):
        return create_organization(owner=owner, slug=slug, name=slug.title(), settings=settings)


def settings_of(organization):
    from skeleton.tenancy.organization_settings import current_settings

    with system_context(reason="test:rules-read", organization_id=organization.pk):
        return current_settings()


def locks_of(organization):
    from skeleton.tenancy.models import OrganizationSettingLock

    with system_context(reason="test:rules-locks"):
        return dict(
            OrganizationSettingLock.objects.filter(locked_organization=organization).values_list("key", "reason")
        )


def test_default_and_locked_rules(platform):
    rule(platform, "organization.default_language", value="pt-br")
    rule(platform, "organization.brand_color", value="#123456", locked=True, reason="Marca do produto")
    organization = new_organization(platform, "rules-new")
    record = settings_of(organization)
    assert (record.default_language, record.brand_color) == ("pt-br", "#123456")
    assert locks_of(organization) == {"organization.brand_color": "Marca do produto"}


def test_invalid_rules_and_guard(platform):
    for kwargs in (
        {"key": "organization.unknown", "value": "x"},
        {"key": "organization.brand_color", "value": "azul"},
        {"key": "organization.brand_color", "value": "#123456", "locked": True, "reason": ""},
        {"key": "organization.default_language"},  # A default without a value.
    ):
        with pytest.raises(ValidationError):
            rule(platform, **kwargs)
    saved = rule(platform, "organization.default_language", value="pt-br")
    with pytest.raises(ConcurrentUpdate):
        rule(platform, "organization.default_language", value="", revision=0)
    other, _ = bootstrap_identity(email="rules-other@example.test", organization="rules-o", name="O", password=PASSWORD)
    demote(other)
    with request_context(actor=other, request_id=uuid4(), source="test:rules-denied"):
        with pytest.raises(PermissionDenied):
            save_rule(actor=other, key="organization.default_language", mode="default", value="", locked=False)
    as_platform(
        platform,
        lambda: remove_rule(actor=platform, key="organization.default_language", expected_revision=saved.revision),
    )
    assert rules() == ()
    again = rule(platform, "organization.default_language", value="")
    assert again.pk == saved.pk  # Saving the key again restores the row.


@pytest.mark.needs_example_app
@pytest.mark.closed_app_defaults
def test_required_platform_field(platform, monkeypatch):
    from skeleton.system import app_registry
    from skeleton.system.app_registry import ConfigField
    from skeleton.system.models import AppInstallation
    from skeleton.tenancy.models import Organization

    fields = (ConfigField("region", str, "sa-east-1", managed_by="platform"),)
    monkeypatch.setitem(
        app_registry._manifests, NOTES, replace(app_registry.get_app(NOTES), configuration_fields=fields)
    )
    rule(platform, f"apps.{NOTES}.region", mode="required", locked=True, reason="Região contratada")
    with pytest.raises(ValidationError) as missing:
        new_organization(platform, "rules-missing")
    assert f"settings.apps.{NOTES}.region" in missing.value.message_dict
    with system_context(reason="test:rules-none"):
        assert not Organization.objects.filter(slug="rules-missing").exists()  # Nothing was created.
    organization = new_organization(platform, "rules-given", settings={f"apps.{NOTES}.region": "us-east-1"})
    with system_context(reason="test:rules-app", organization_id=organization.pk):
        record = AppInstallation.objects.get(app_label=NOTES)
    assert (record.enabled, record.configuration) == (False, {"region": "us-east-1"})
    assert locks_of(organization) == {}  # A platform field is the platform's already: nothing to lock.


def test_apply_to_existing_skips_locked(platform):
    from skeleton.tenancy.setting_locks import lock_setting

    first = new_organization(platform, "rules-first")
    second = new_organization(platform, "rules-second")
    as_platform(
        platform,
        lambda: lock_setting(
            actor=platform, organization=second, key="organization.display_name", value="Fixo", reason="Contrato"
        ),
    )
    from skeleton.testing.jobs import run_pending_jobs

    saved = rule(platform, "organization.display_name", value="Produto")
    assert as_platform(platform, lambda: enqueue_apply(actor=platform, rule=saved)) >= 3
    as_platform(platform, lambda: enqueue_apply(actor=platform, rule=saved))  # The same revision: no new jobs.
    run_pending_jobs()
    assert settings_of(first).display_name == "Produto"
    assert settings_of(second).display_name == "Fixo"


def test_unknown_key_is_skipped(platform):
    from skeleton.tenancy.models import OrganizationProvisioningRule

    with system_context(reason="test:rules-orphan"):
        OrganizationProvisioningRule(key="apps.gone_app.enabled", mode="default", value=True).save()
    organization = new_organization(platform, "rules-orphan")  # Created anyway; the rule is ignored.
    assert organization.pk


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def test_pages(platform):
    client = login(platform)
    url = reverse("platform:organization-policy")

    def post(target, data):
        return client.post(target, {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})

    assert client.get(url).status_code == 200
    data = {"key": "organization.display_name", "mode": "required", "value": "", "reason": "", "expected_revision": 0}
    assert post(url, {**data, "locked": "on"}).status_code == 400  # Locked without a reason.
    assert post(url, data).status_code == 302
    create = reverse("platform:organization-create")
    form = client.get(create).context["form"]
    [name] = [name for name in form.fields if name.startswith("setting_")]
    with system_context(reason="test:rules-owner"):
        create_identity(email="page-owner@rules.example.test", password=PASSWORD)
    base = {"name": "Pela página", "slug": "rules-page", "owner": "page-owner@rules.example.test"}
    assert post(create, base).status_code == 400  # The required value is missing.
    assert post(create, {**base, name: "Nome do produto"}).status_code == 302
    from skeleton.tenancy.models import Organization

    with system_context(reason="test:rules-page"):
        organization = Organization.objects.get(slug="rules-page")
    assert settings_of(organization).display_name == "Nome do produto"
