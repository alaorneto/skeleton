"""Connections of a central instance to remote ones (#712), with a fake remote (no network)."""

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.integrations.remote_instances import services
from skeleton.service_api.errors import Conflict, NotFound, Unavailable
from skeleton.service_api.remote_instances import RemoteInstanceService
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.remote_instances import fake_remote, organization
from skeleton.testing.service_api import assert_service_contract

pytestmark = [pytest.mark.usefixtures("skeleton_db")]
PASSWORD = "Remote-instances-Password!712"


@pytest.fixture
def setup(settings):
    settings.REMOTE_INSTANCE_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    platform, org_a = bootstrap_identity(
        email="remote-platform@example.test", organization="remote-a", name="A", password=PASSWORD
    )
    owner_b, org_b = bootstrap_identity(
        email="remote-b@example.test", organization="remote-b", name="B", password=PASSWORD
    )
    with system_context(reason="test:remote-demote"):
        owner_b.is_staff = owner_b.is_superuser = False
        owner_b.save(update_fields=["is_staff", "is_superuser"])
    return platform, org_a, owner_b, org_b


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:remote"):
        return fn()


def save(platform, **kwargs):
    kwargs.setdefault("label", "Produto X")
    kwargs.setdefault("base_url", "https://x.example.test")
    return as_platform(platform, lambda: services.save_connection(actor=platform, **kwargs))


def test_connection_validation_and_secret(setup):
    from django.db import connection as db

    platform, org_a, owner_b, _ = setup
    for url in ("http://x.example.test", "https://x.example.test/api", "https://user:pw@x.example.test"):
        with pytest.raises(ValidationError):
            save(platform, base_url=url, token="skt_secret")
    row = save(platform, token="skt_secret_value", granted_organization=org_a)
    with db.cursor() as cursor:
        cursor.execute("SELECT token FROM sk_integrations_remoteinstanceconnection WHERE sysid = %s", [row.pk])
        assert "skt_secret_value" not in cursor.fetchone()[0]
    edited = save(platform, token="", label="Renomeada", sysid=row.pk, expected_revision=row.revision)
    assert edited.token.reveal() == "skt_secret_value"
    with request_context(actor=owner_b, request_id=uuid4(), source="test:remote-denied"):
        with pytest.raises(PermissionDenied):
            services.save_connection(actor=owner_b, label="x", base_url="https://y.example.test", token="t")


def test_test_pins_the_instance_and_detects_a_swap(setup):
    platform, org_a, _, _ = setup
    row = save(platform, token="skt_t", granted_organization=org_a)
    remote_id = uuid4()
    with fake_remote(instance_id=remote_id):
        assert as_platform(platform, lambda: services.test_connection(actor=platform, sysid=row.pk)) == ""
    row.refresh_from_db()
    assert row.expected_instance_id == remote_id
    with fake_remote(instance_id=uuid4()):
        assert (
            as_platform(platform, lambda: services.test_connection(actor=platform, sysid=row.pk))
            == "remote_instance_mismatch"
        )
    with fake_remote(instance_id=remote_id) as remote:
        remote.status = 401
        assert (
            as_platform(platform, lambda: services.test_connection(actor=platform, sysid=row.pk))
            == "remote_unauthorized"
        )
        remote.status = 503
        assert (
            as_platform(platform, lambda: services.test_connection(actor=platform, sysid=row.pk))
            == "remote_unavailable"
        )
        remote.status = None
        remote.body = b"x" * (1024 * 1024 + 10)
        assert (
            as_platform(platform, lambda: services.test_connection(actor=platform, sysid=row.pk))
            == "remote_invalid_response"
        )


def test_service_reads_in_jobs_of_the_granted_organization(setup):
    platform, org_a, owner_b, org_b = setup
    assert_service_contract(RemoteInstanceService)
    row = save(platform, token="skt_t", granted_organization=org_a)
    remotes = [organization(f"Cliente{index}") for index in range(3)]
    with fake_remote(organizations=remotes):
        with system_context(reason="test:remote-job", organization_id=org_a.pk):
            service = RemoteInstanceService()
            [info] = service.connections(actor=None)
            assert info.sysid == row.pk
            first = service.organizations(actor=None, connection_id=row.pk)
            second = service.organizations(actor=None, connection_id=row.pk, cursor=first.next_cursor)
            assert len(first.items) == 2 and len(second.items) == 1 and second.next_cursor is None
            assert {item.name for item in (*first.items, *second.items)} == {"Cliente0", "Cliente1", "Cliente2"}
        with system_context(reason="test:remote-job-b", organization_id=org_b.pk):
            with pytest.raises(NotFound):
                RemoteInstanceService().organizations(actor=None, connection_id=row.pk)
        with tenant_context(organization_id=org_a.pk, actor=platform, source="test:remote-request"):
            with pytest.raises(Unavailable):
                RemoteInstanceService().organizations(actor=platform, connection_id=row.pk)
    with fake_remote() as remote:
        remote.status = 403
        with system_context(reason="test:remote-forbidden", organization_id=org_a.pk):
            with pytest.raises(Conflict) as refused:
                RemoteInstanceService().organizations(actor=None, connection_id=row.pk)
    assert refused.value.code == "remote_forbidden"


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
    from skeleton.integrations.remote_instances.models import RemoteInstanceConnection

    platform, org_a, owner_b, _ = setup
    client = login(platform)

    def post(url, data):
        return client.post(url, {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})

    data = {
        "label": "X",
        "base_url": "https://x.example.test",
        "token": "skt_page",
        "granted_organization": str(org_a.pk),
        "expected_revision": 0,
    }
    assert post(reverse("platform:remote-instances"), {**data, "base_url": "http://x"}).status_code == 400
    assert post(reverse("platform:remote-instances"), data).status_code == 302
    row = RemoteInstanceConnection.objects.get(label="X")
    page = client.get(reverse("platform:remote-instance", args=[row.pk]))
    assert page.status_code == 200 and "skt_page" not in page.content.decode()
    with fake_remote():
        tested = post(reverse("platform:remote-instance-test", args=[row.pk]), {})
    assert tested.status_code == 200 and "bem-sucedida" in tested.context["result"]
    assert login(owner_b).get(reverse("platform:remote-instances")).status_code in (403, 404)
