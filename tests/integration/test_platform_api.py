"""Management API of the instance (#708-#711): identity, platform clients and tokens, insights, routes.

PostgreSQL with the runtime role. A ``platform`` token opens only the platform views, with its
scope and from its networks; the snapshot of each organization follows its data; the API pages
the organizations by a signed cursor and hides billing without the scope.
"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.integrations.tokens import platform as clients
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.people import add_member

pytestmark = [pytest.mark.usefixtures("skeleton_db")]
PASSWORD = "Platform-api-Password!711"
ALL_SCOPES = [
    "platform.instance:read",
    "platform.organizations:read",
    "platform.billing:read",
    "platform.billing_contacts:read",
    "platform.metrics:read",
]


@pytest.fixture(autouse=True)
def clean_rate_limit():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def setup():
    platform, org_a = bootstrap_identity(
        email="papi-platform@example.test", organization="papi-a", name="A", password=PASSWORD
    )
    owner_b, org_b = bootstrap_identity(email="papi-b@example.test", organization="papi-b", name="B", password=PASSWORD)
    with system_context(reason="test:papi-demote"):
        owner_b.is_staff = owner_b.is_superuser = False
        owner_b.save(update_fields=["is_staff", "is_superuser"])
    return platform, org_a, owner_b, org_b


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:papi"):
        return fn()


def token(platform, scopes=ALL_SCOPES, networks=()):
    client = as_platform(platform, lambda: clients.create_platform_client(actor=platform, name="Central"))
    issued = as_platform(
        platform,
        lambda: clients.issue_platform_token(
            actor=platform,
            client_id=client.pk,
            name="sync",
            scopes=list(scopes),
            expires_at=timezone.now() + timedelta(days=30),
            allowed_networks=list(networks),
        ),
    )
    return client, issued


def bearer(text, **extra):
    return Client(HTTP_AUTHORIZATION=f"Bearer {text}", **extra)


def refresh(organization):
    from skeleton.insights.collect import refresh_organization

    with system_context(reason="test:papi-refresh", organization_id=organization.pk):
        return refresh_organization()


def test_instance_identity_is_one_stable_row(setup):
    from skeleton.system.instance_identity import instance_identity, update_instance_identity
    from skeleton.system.models import InstanceIdentity

    platform, _, owner_b, _ = setup
    with system_context(reason="test:papi-identity"):
        assert InstanceIdentity.all_objects.count() == 1
    first = instance_identity()
    updated = as_platform(
        platform,
        lambda: update_instance_identity(
            actor=platform, name="Produto X", purpose="Vendas", expected_revision=first.revision
        ),
    )
    assert (updated.instance_id, updated.name, updated.purpose) == (first.instance_id, "Produto X", "Vendas")
    with pytest.raises(ConcurrentUpdate):
        as_platform(
            platform,
            lambda: update_instance_identity(actor=platform, name="Y", purpose="", expected_revision=first.revision),
        )
    with request_context(actor=owner_b, request_id=uuid4(), source="test:papi-denied"):
        with pytest.raises(PermissionDenied):
            update_instance_identity(actor=owner_b, name="Z", purpose="", expected_revision=updated.revision)
    from skeleton.common.exceptions import ImmutableRecord

    with system_context(reason="test:papi-immutable"):
        row = InstanceIdentity.objects.get()
        row.instance_id = uuid4()
        with pytest.raises((ImmutableRecord, ValidationError)):
            row.save()
    assert "instance_id" not in json.dumps(Client().get(reverse("api:server")).json())


def test_platform_token_reaches_only_platform_views(setup, settings):
    platform, org_a, owner_b, _ = setup
    _, issued = token(platform, scopes=["platform.instance:read"])
    api = bearer(issued.text)
    instance = api.get(reverse("api:platform-instance"))
    assert instance.status_code == 200 and instance["Cache-Control"] == "no-store"
    body = instance.json()
    assert {"instance_id", "name", "product_name", "core_version", "billing_enabled", "apps"} <= set(body)
    assert api.get(reverse("api:platform-organizations")).status_code == 403  # Scope missing.
    for url in (reverse("api:organizations"), reverse("api:own-membership", args=[org_a.pk])):
        answer = api.get(url)
        assert answer.status_code == 403 and answer.json()["code"] == "token_not_allowed"
    # Other credentials on a platform view.
    from skeleton.integrations.tokens.services import issue_personal_token

    with request_context(actor=platform, request_id=uuid4(), source="test:papi-personal"):
        personal = issue_personal_token(
            actor=platform, name="p", scopes=["files:read"], expires_at=timezone.now() + timedelta(days=1)
        )
    refused = bearer(personal.text).get(reverse("api:platform-instance"))
    assert refused.status_code == 403 and refused.json()["code"] == "token_not_allowed"
    member = Client()
    member.force_login(owner_b)
    assert member.get(reverse("api:platform-instance")).status_code == 404
    admin = Client()
    admin.force_login(platform)
    assert admin.get(reverse("api:platform-instance")).status_code == 200


def test_networks_revocation_and_deactivation(setup):
    platform, _, _, _ = setup
    client, issued = token(platform, scopes=["platform.instance:read"], networks=["203.0.113.0/24"])
    url = reverse("api:platform-instance")
    outside = bearer(issued.text).get(url, REMOTE_ADDR="198.51.100.7")
    assert outside.status_code == 401 and outside.json()["code"] == "invalid_token"
    assert bearer(issued.text).get(url, REMOTE_ADDR="203.0.113.9").status_code == 200
    as_platform(platform, lambda: clients.deactivate_platform_client(actor=platform, client_id=client.pk))
    assert bearer(issued.text).get(url, REMOTE_ADDR="203.0.113.9").status_code == 401
    other, second = token(platform, scopes=["platform.instance:read"])
    row = second.token
    as_platform(
        platform, lambda: clients.revoke_platform_token(actor=platform, token_id=row.pk, expected_revision=row.revision)
    )
    assert bearer(second.text).get(url).status_code == 401


def test_platform_clients_and_scopes_are_fenced(setup):
    from skeleton.integrations.tokens.scopes import validate_scopes
    from skeleton.tenancy.models import Membership

    platform, org_a, owner_b, _ = setup
    client, _ = token(platform)
    assert client.is_service_account and client.is_platform_client and not client.is_staff
    with pytest.raises(ValidationError):
        validate_scopes(["platform.instance:read"], feed=False)  # Never on another kind of token.
    with pytest.raises(ValidationError):
        as_platform(
            platform,
            lambda: clients.issue_platform_token(
                actor=platform,
                client_id=client.pk,
                name="x",
                scopes=["files:read"],
                expires_at=timezone.now() + timedelta(days=1),
            ),
        )
    with pytest.raises(ValidationError):
        clients.validate_networks(["not-a-network"])
    with system_context(reason="test:papi-member", organization_id=org_a.pk):
        with pytest.raises(ValidationError):
            Membership(organization=org_a, user=client).full_clean()
    with request_context(actor=owner_b, request_id=uuid4(), source="test:papi-denied"):
        with pytest.raises(PermissionDenied):
            clients.create_platform_client(actor=owner_b, name="x")


def test_snapshots_follow_the_organization(setup):
    from skeleton.insights.models import OrganizationSnapshot

    platform, org_a, owner_b, org_b = setup
    add_member(organization=org_a, user=create_identity(email="papi-ana@example.test", password=PASSWORD))
    assert refresh(org_a) == "created" and refresh(org_b) == "created"
    with system_context(reason="test:papi-read"):
        a = OrganizationSnapshot.all_objects.get(subject_organization=org_a)
        b = OrganizationSnapshot.all_objects.get(subject_organization=org_b)
    assert (a.members_active, b.members_active, a.billing) == (2, 1, {})
    assert refresh(org_a) == "unchanged"
    with system_context(reason="test:papi-read"):
        assert OrganizationSnapshot.all_objects.get(pk=a.pk).updated_at == a.updated_at
    add_member(organization=org_a, user=create_identity(email="papi-bia@example.test", password=PASSWORD))
    assert refresh(org_a) == "updated"
    with system_context(reason="test:papi-read"):
        after = OrganizationSnapshot.all_objects.get(pk=a.pk)
        assert (after.members_active, after.data_version) == (3, 2) and after.updated_at > a.updated_at
        assert OrganizationSnapshot.all_objects.get(pk=b.pk).updated_at == b.updated_at


def test_metrics_and_app_collectors(setup):
    from skeleton.insights import collect
    from skeleton.insights.models import InstanceMetric

    platform, org_a, _, org_b = setup
    collect._collectors.pop("acme", None)
    collect.register_instance_metric("acme", lambda organization_id: {"widgets": 2})
    try:
        refresh(org_a)
        refresh(org_b)
        with system_context(reason="test:papi-metrics"):
            collect.refresh_metrics(timezone.now().date())
            values = {(row.code, row.currency): row.value for row in InstanceMetric.all_objects.all()}
    finally:
        collect._collectors.pop("acme", None)
    assert values[("acme.widgets", "")] == 4
    assert values[("organizations.total", "")] >= 2
    assert not any(code.startswith("billing.") for code, _ in values)  # Billing off: no billing metric.


def test_billing_reporting_mrr():
    from types import SimpleNamespace

    from skeleton.billing.reporting import monthly_amount

    assert monthly_amount(SimpleNamespace(unit_amount=9900, interval="month", interval_count=1), 2) == 19800
    assert monthly_amount(SimpleNamespace(unit_amount=120000, interval="year", interval_count=1), 1) == 10000


def test_organizations_api_pages_by_signed_cursor(setup):
    platform, org_a, _, org_b = setup
    for organization in (org_a, org_b):
        refresh(organization)
    _, issued = token(platform, scopes=["platform.organizations:read", "platform.metrics:read"])
    api = bearer(issued.text)
    url = reverse("api:platform-organizations")
    first = api.get(url, {"page_size": 1}).json()
    assert len(first["results"]) == 1 and first["next_cursor"]
    assert "billing" not in first["results"][0]  # Without platform.billing:read.
    second = api.get(url, {"page_size": 1, "cursor": first["next_cursor"]}).json()
    assert {first["results"][0]["id"], second["results"][0]["id"]} == {str(org_a.pk), str(org_b.pk)}
    forged = api.get(url, {"cursor": first["next_cursor"][:-3] + "abc"})
    assert forged.status_code == 400 and forged.json()["code"] == "invalid_cursor"
    since = max(first["results"][0]["updated_at"], second["results"][0]["updated_at"])
    assert api.get(url, {"updated_since": since}).json()["results"] == []
    assert api.get(url, {"unknown": "1"}).json()["code"] == "invalid_filter"
    assert api.get(reverse("api:platform-organization", args=[uuid4()])).status_code == 404
    assert api.get(reverse("api:platform-organization-invoices", args=[org_a.pk])).status_code == 403
    metrics = api.get(reverse("api:platform-metrics"), {"since": "2025-01-01", "until": "2026-12-31"})
    assert metrics.status_code == 400  # More than 400 days.
    assert api.get(reverse("api:platform-metrics"), {"code": "billing.*"}).json() == {"results": []}


def test_billing_routes_need_their_scopes(setup, settings):
    platform, org_a, _, _ = setup
    refresh(org_a)
    _, issued = token(platform)
    api = bearer(issued.text)
    invoices = api.get(reverse("api:platform-organization-invoices", args=[org_a.pk]))
    assert invoices.status_code == 200 and invoices.json()["results"] == []
    assert api.get(reverse("api:platform-organization-payments", args=[org_a.pk])).status_code == 200
    # Billing off: no billing account to show.
    assert api.get(reverse("api:platform-organization-billing-account", args=[org_a.pk])).status_code == 404


def test_openapi_lists_platform_routes_for_the_platform_only(setup):
    platform, _, owner_b, _ = setup
    admin = Client()
    admin.force_login(platform)
    schema = admin.get(reverse("api:openapi")).json()
    platform_paths = [path for path in schema["paths"] if "/platform/" in path]
    assert platform_paths
    tags = {operation["tags"][0] for path in platform_paths for operation in schema["paths"][path].values()}
    assert tags == {"Plataforma"}
    member = Client()
    member.force_login(owner_b)
    assert not [path for path in member.get(reverse("api:openapi")).json()["paths"] if "/platform/" in path]


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def test_pages(setup):
    platform, org_a, owner_b, _ = setup
    client = login(platform)

    def post(url, data):
        return client.post(url, {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})

    assert post(reverse("platform:api-clients"), {"name": "Central"}).status_code == 302
    created = clients.platform_clients().get(first_name="Central")
    page = post(
        reverse("platform:api-client", args=[created.pk]),
        {"name": "sync", "scopes": ["platform.instance:read"], "days": 30, "allowed_networks": ""},
    )
    assert (
        page.status_code == 200
        and page.context["issued"].startswith("skt_")
        and page["Cache-Control"].startswith("no-store")
        or "no-cache" in page["Cache-Control"]
    )
    assert client.get(reverse("platform:api-client", args=[created.pk])).context["issued"] is None
    refresh(org_a)
    assert client.get(reverse("platform:insights")).status_code == 200
    instance = client.get(reverse("platform:instance"))
    assert instance.status_code == 200 and str(instance.context["identity"].instance_id) in instance.content.decode()
    assert login(owner_b).get(reverse("platform:api-clients")).status_code in (403, 404)
