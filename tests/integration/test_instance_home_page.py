"""The public home page of the instance (#622), on PostgreSQL with the runtime role."""

import logging
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.common.exceptions import ConcurrentUpdate
from tests.integration.appearance_support import PASSWORD, acting, login, member, platform_admin

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_home_page")
DOCUMENT = {
    "version": 1,
    "blocks": [
        {"type": "hero", "title": "Plataforma Exemplo", "subtitle": "Bem-vindo", "login_button": True},
        {"type": "text", "heading": "Sobre", "body": "<script>alert(1)</script> texto"},
    ],
}


def post(client, url, data=None):
    return client.post(url, {**(data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def publish(admin, document=DOCUMENT, *, published=True, revision=0):
    from skeleton.appearance.pages import save_home_page

    with acting(admin):
        return save_home_page(actor=admin, document=document, published=published, expected_revision=revision)


def test_anonymous_root_goes_to_the_login_until_a_page_is_published():
    anonymous = Client()
    response = anonymous.get("/")
    assert response.status_code == 302 and response["Location"] == f"{reverse('account_login')}?next=/"
    admin = platform_admin()
    state = publish(admin, published=False)
    assert anonymous.get("/").status_code == 302  # A draft is never public.
    publish(admin, revision=state.revision)
    response = anonymous.get("/")
    html = response.content.decode()
    assert response.status_code == 200
    assert "Plataforma Exemplo" in html and f'href="{reverse("account_login")}"' in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; texto" in html and "<script>alert" not in html
    assert "no-cache" in response["Cache-Control"] and "no-store" in response["Cache-Control"]
    # Signed in, "/" stays "my organizations".
    signed_in = login(admin.email)
    page = signed_in.get("/")
    assert page.status_code == 200 and "Plataforma Exemplo" not in page.content.decode()
    assert page.templates[0].name == "skeleton/web/home.html"


def test_unpublishing_brings_the_login_back():
    admin = platform_admin()
    state = publish(admin)
    assert Client().get("/").status_code == 200
    publish(admin, published=False, revision=state.revision)
    assert Client().get("/").status_code == 302


def test_the_organization_host_never_shows_the_platform_page(settings):
    from tests.site_support import PLATFORM_HOST, site_settings

    site_settings(settings)
    publish(platform_admin())
    assert Client(HTTP_HOST=PLATFORM_HOST).get("/").status_code == 200
    # An unknown or disabled organization host is the neutral 404, never the platform page.
    response = Client(HTTP_HOST="nobody.example.test").get("/")
    assert response.status_code == 404 and "Plataforma Exemplo" not in response.content.decode()


def test_only_the_full_platform_administrator_edits(subtests):
    from skeleton.accounts.platform import grant_platform_role
    from skeleton.tenancy.context import request_context

    admin = platform_admin()
    auditor = member("home-auditor@example.test")
    with request_context(actor=admin, request_id=uuid4(), source="test:home-page-roles"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    url = reverse("platform:appearance-home")
    add = reverse("platform:appearance-home-add") + "?type=text"
    with subtests.test("anonymous"):
        response = Client().get(url)
        assert response.status_code == 302 and reverse("account_login") in response["Location"]
    for who, email in (("member", member().email), ("auditor", auditor.email)):
        with subtests.test(who):
            other = login(email, PASSWORD)
            assert other.get(url).status_code == 403
            assert post(other, add, {"body": "x", "revision": 0}).status_code == 403
    with subtests.test("service"):
        with acting(auditor), pytest.raises(PermissionDenied):
            from skeleton.appearance.pages import save_home_page

            save_home_page(actor=auditor, document=DOCUMENT, published=True, expected_revision=0)


def test_the_editor_builds_previews_and_publishes_the_page():
    from skeleton.appearance.pages import current_home_page

    admin = platform_admin()
    client = login(admin.email)
    assert client.get(reverse("platform:appearance-home")).status_code == 200
    hero = post(
        client,
        reverse("platform:appearance-home-add") + "?type=hero",
        {"title": "Olá", "subtitle": "", "login_button": "on", "revision": 0},
    )
    assert hero.status_code == 302
    links = post(
        client,
        reverse("platform:appearance-home-add") + "?type=links",
        {"heading": "", "items": "Blog | https://exemplo.test/blog", "revision": 1},
    )
    assert links.status_code == 302
    preview = client.get(reverse("platform:appearance-home-preview"))
    assert preview.status_code == 200 and "Pré-visualização" in preview.content.decode()
    assert 'rel="noopener noreferrer"' in preview.content.decode()
    assert Client().get("/").status_code == 302  # Not published yet.
    assert (
        post(client, reverse("platform:appearance-home-publish"), {"published": "on", "revision": 2}).status_code == 302
    )
    state = current_home_page()
    assert state.published and [block["type"] for block in state.blocks] == ["hero", "links"]
    assert Client().get("/").status_code == 200  # The writing process sees it at once.
    stale = post(client, reverse("platform:appearance-home-publish"), {"published": "off", "revision": 2})
    assert stale.status_code == 409 and "alterada por outra pessoa" in stale.content.decode()
    bad = post(
        client,
        reverse("platform:appearance-home-add") + "?type=links",
        {"heading": "", "items": "X | javascript:alert(1)", "revision": 3},
    )
    assert bad.status_code == 400 and "error-summary" in bad.content.decode()
    assert current_home_page().revision == 3


def test_writes_require_a_recent_authentication():
    admin = platform_admin()
    client = login(admin.email)
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = post(client, reverse("platform:appearance-home-add") + "?type=text", {"body": "x", "revision": 0})
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    assert client.get(reverse("platform:appearance-home")).status_code == 200  # The editor still renders.


def test_service_cas_validation_audit_and_security_event(caplog):
    from skeleton.appearance.models import InstancePage
    from skeleton.audit.models import AuditEvent

    admin = platform_admin()
    with caplog.at_level(logging.INFO, logger="skeleton.security"):
        state = publish(admin)
    with pytest.raises(ConcurrentUpdate):
        publish(admin, revision=0)
    with pytest.raises(ValidationError) as error:
        publish(admin, {"version": 1, "blocks": [{"type": "widget", "name": "x"}]}, revision=state.revision)
    assert "blocks.0.type" in error.value.message_dict
    with pytest.raises(ValidationError):
        publish(admin, {"version": 1, "blocks": []}, revision=state.revision)  # Published needs a block.
    assert publish(admin, revision=state.revision).revision == state.revision  # Equal values write nothing.
    events = [record for record in caplog.records if getattr(record, "event", "") == "appearance.home_page_changed"]
    assert len(events) == 1 and events[0].reason_code == "published"
    assert "Plataforma Exemplo" not in caplog.text
    with acting(admin):
        row = InstancePage.objects.get()
        audit = list(AuditEvent.objects.filter(record_sysid=row.pk))
    assert [event.operation for event in audit] == ["create"]


def test_the_cache_keeps_the_last_good_page_when_the_database_fails(monkeypatch):
    from django.db import DatabaseError

    from skeleton.appearance import pages

    publish(platform_admin())
    assert pages.home_page().published
    monkeypatch.setattr(pages, "_CACHE", {**pages._CACHE, "at": 0.0})  # The TTL expired.

    def down():
        raise DatabaseError("down")

    monkeypatch.setattr(pages, "current_home_page", down)
    assert pages.home_page().published  # Last good value.
    monkeypatch.setattr(pages, "_CACHE", {**pages._CACHE, "at": 0.0, "good": None})
    assert not pages.home_page().published  # Nothing good to keep: no page, the login.
