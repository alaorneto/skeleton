"""Regressions for recovered extension, delivery and pagination findings."""
import importlib
import json
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib import admin
from django.core.mail import EmailMultiAlternatives
from django.test import Client
from django.urls import clear_url_caches, reverse
from django.test.utils import isolate_apps

from skeleton.accounts.models import User
from skeleton.accounts.services import bootstrap_identity
from skeleton.tenancy.context import tenant_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Recovered-Surface-Password!42"


def login(user, *, suppress_errors=False):
    client = Client(enforce_csrf_checks=True, raise_request_exception=not suppress_errors)
    assert client.get(reverse("account_login")).status_code == 200
    assert client.post(reverse("account_login"), {
        "login": user.email, "password": PASSWORD,
        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
    }).status_code == 302
    return client


def api_post(client, url, values, revision=None):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    if revision is not None:
        headers["HTTP_IF_MATCH"] = revision
    return client.post(url, json.dumps(values), content_type="application/json", **headers)


@isolate_apps()
def test_ea02_conventional_extension_admin_registration_is_routed():
    class ExtensionIdentity(User):
        class Meta:
            proxy = True
            app_label = "sk_accounts"

    @admin.register(ExtensionIdentity)
    class ExtensionIdentityAdmin(admin.ModelAdmin):
        def has_add_permission(self, request):
            return False

    try:
        # Conventional global record editors remain protected, outside configuration admin.
        from config import urls
        importlib.reload(urls)
        clear_url_caches()
        owner, _ = bootstrap_identity(email="extension-admin@example.test", organization="extension-admin", name="Extension", password=PASSWORD)
        from skeleton.tenancy.context import system_context
        with system_context(reason="test:platform-admin"):
            owner.is_staff = owner.is_superuser = True
            owner.save(update_fields=["is_staff", "is_superuser"])
        client = login(owner)
        url = reverse("admin:sk_accounts_extensionidentity_changelist")
        assert url.startswith("/platform/records/")
        response = client.get(url)
        assert response.status_code == 200
        assert owner.email.encode() in response.content
        assert client.get("/admin/sk_accounts/extensionidentity/").status_code == 404
        from skeleton.accounts.services import create_identity
        from allauth.account.models import EmailAddress
        with system_context(reason="test:ordinary-extension-user"):
            ordinary = create_identity(email="ordinary-admin@example.test", password=PASSWORD)
            EmailAddress.objects.create(user=ordinary, email=ordinary.email, verified=True, primary=True)
        assert login(ordinary).get(reverse("admin:sk_accounts_extensionidentity_changelist")).status_code == 403
    finally:
        admin.site.unregister(ExtensionIdentity)
        from config import urls
        importlib.reload(urls)
        clear_url_caches()


@pytest.mark.parametrize("resource", ["socialaccount/socialtoken", "socialaccount/socialapp", "mfa/authenticator", "auth/group"])
def test_ea02_technical_secret_admins_remain_unrouted(resource):
    owner, _ = bootstrap_identity(email="restricted-admin@example.test", organization="restricted-admin", name="Restricted", password=PASSWORD)
    response = login(owner).get(f"/admin/{resource}/")
    assert response.status_code == 404


def test_sbr08_smtp_failure_preserves_invitation_and_reports_delivery(caplog, monkeypatch):
    owner, organization = bootstrap_identity(email="delivery-owner@example.test", organization="delivery", name="Delivery", password=PASSWORD)
    client = login(owner, suppress_errors=True)
    from skeleton.access.models import Role
    from skeleton.tenancy.models import Invitation
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:delivery"):
        role = Role.objects.get(code="member")

    def unavailable(self, fail_silently=False):
        raise OSError("secret-smtp-link-password-sentinel")

    monkeypatch.setattr(EmailMultiAlternatives, "send", unavailable)
    collection = reverse("api:access-list", kwargs={"organization_id": organization.pk, "resource": "invitations"})
    response = api_post(client, collection, {"email": "recipient@example.test", "role": str(role.pk)})
    assert response.status_code == 201
    assert response["X-Email-Delivery"] == "failed"
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:delivery-observe"):
        invitation = Invitation.objects.get(pk=response.json()["sysid"])
        assert invitation.accepted_at is None
        assert invitation.email == "recipient@example.test"
    failures = [record for record in caplog.records if getattr(record, "event", None) == "mail.delivery_failed"]
    assert len(failures) == 1
    assert failures[0].error_class == "OSError"
    assert "secret-smtp-link-password-sentinel" not in failures[0].getMessage()


def test_sbr08_invitation_resend_replaces_token_and_rejects_old_link(mailoutbox):
    owner, organization = bootstrap_identity(email="resend-owner@example.test", organization="resend", name="Resend\nOrganization", password=PASSWORD)
    client = login(owner, suppress_errors=True)
    from skeleton.access.models import Role
    from skeleton.tenancy.models import Invitation
    from skeleton.tenancy.invitations import InvalidInvitation, validate_invitation
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:resend"):
        role = Role.objects.get(code="member")
    collection = reverse("api:access-list", kwargs={"organization_id": organization.pk, "resource": "invitations"})
    first = api_post(client, collection, {"email": "recipient@example.test", "role": str(role.pk)})
    assert first.status_code == 201
    first_link = re.search(r"https?://[^\s]+/invite/[^\s]+", mailoutbox[-1].body).group()
    old_token = urlsplit(first_link).path.rstrip("/").split("/")[-1]
    old_id = first.json()["sysid"]
    resend = f"/api/v1/o/{organization.pk}/invitations/{old_id}/resend/"
    assert api_post(client, resend, {}, '"99"').status_code == 412
    renewed = api_post(client, resend, {}, first["ETag"])
    assert renewed.status_code == 201
    new_link = re.search(r"https?://[^\s]+/invite/[^\s]+", mailoutbox[-1].body).group()
    new_token = urlsplit(new_link).path.rstrip("/").split("/")[-1]
    assert renewed.json()["sysid"] != old_id
    assert new_token != old_token
    with pytest.raises(InvalidInvitation):
        validate_invitation(organization.pk, old_token)
    assert str(validate_invitation(organization.pk, new_token).sysid) == renewed.json()["sysid"]
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:resend-observe"):
        assert Invitation.objects.with_deleted().get(pk=old_id).deleted_at is not None
        assert Invitation.objects.filter(email="recipient@example.test").count() == 1
    assert api_post(client, resend, {}, first["ETag"]).status_code == 404


def test_sbr14_pagination_retains_validated_filter_and_results():
    from examples.notes.models import Note
    owner, organization = bootstrap_identity(email="pages-owner@example.test", organization="pages", name="Pages", password=PASSWORD)
    client = login(owner)
    title = "Filtered & escaped <title>"
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:pagination"):
        Note.objects.bulk_create([
            Note(organization=organization, owner=owner, code=f"filtered-{number}", title=title)
            for number in range(26)
        ] + [Note(organization=organization, owner=owner, code="excluded", title="Excluded result")])
    collection = reverse("web:records", kwargs={"organization_id": organization.pk, "table_key": "example_notes.note"})
    first = client.get(collection, {"title": title})
    assert first.status_code == 200
    href = unescape(re.search(r'href="([^"]+)">Próxima', first.content.decode()).group(1))
    assert parse_qs(urlsplit(href).query) == {"title": [title], "page": ["2"]}
    second = client.get(collection + href)
    assert second.status_code == 200
    assert b"Excluded result" not in second.content
    assert len(second.context["rows"].object_list) == 1
    previous = unescape(re.search(r'href="([^"]+)">Anterior', second.content.decode()).group(1))
    assert parse_qs(urlsplit(previous).query) == {"title": [title], "page": ["1"]}
