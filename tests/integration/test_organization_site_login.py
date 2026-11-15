"""Login on the organization address (#486): text, providers per organization, SSO by the main domain."""

from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from django.test import Client, override_settings

from skeleton.accounts import identity_providers
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests.site_support import enable_site, host_of, sites  # noqa: F401

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
KEY = Fernet.generate_key().decode()


@pytest.fixture
def scenario(sites):  # noqa: F811
    from examples.notes.models import Note

    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[KEY]):
        scenario = build_service_scenario(Note, slug="signin")
        enable_site(scenario, scenario.a, display_name="Acme")
        enable_site(scenario, scenario.b, display_name="Beta")
        for organization, provider_id, name in ((scenario.a, "sso-a", "SSO de A"), (scenario.b, "sso-b", "SSO de B")):
            with system_context(reason="test:site-login-provider"):
                identity_providers.create_identity_provider(
                    actor=None,
                    data={
                        "provider_id": provider_id,
                        "protocol": "oidc",
                        "bound_organization": organization,
                        "issuer": f"https://login.example.invalid/realms/{provider_id}",
                        "client_id": f"{provider_id}-client",
                        "secret": "fixture-secret",
                        "name": name,
                        "show_on_login": True,
                    },
                )
        yield scenario


def post_login(client, user, **extra):
    client.get("/accounts/login/", extra.get("query", {}))
    data = {"login": user.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    data.update(extra.get("data", {}))
    path = "/accounts/login/" + ("?" + extra["raw_query"] if "raw_query" in extra else "")
    return client.post(path, data)


def test_password_login_on_the_host_goes_to_the_dashboard_or_next(scenario):
    client = Client(enforce_csrf_checks=True, HTTP_HOST=host_of(scenario.a))
    assert post_login(client, scenario.member)["Location"] == "/"
    other = Client(enforce_csrf_checks=True, HTTP_HOST=host_of(scenario.a))
    response = post_login(other, scenario.member, data={"next": "/tables/example_notes.note/"})
    assert response["Location"] == "/tables/example_notes.note/"
    assert other.get("/").status_code == 200


def test_login_page_on_the_host_speaks_of_the_organization_and_lists_its_providers(scenario):
    page = Client(HTTP_HOST=host_of(scenario.a)).get("/accounts/login/?next=/tables/x/").content.decode()
    assert "Use sua conta para acessar Acme." in page and "organizações" not in page
    assert "SSO de A" in page and "SSO de B" not in page
    link = next(part for part in page.split('href="')[1:] if "sso-a" in part).split('"', 1)[0]
    link = link.replace("&amp;", "&")
    parts = urlsplit(link)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "app.example.test", "/accounts/oidc/sso-a/login/")
    query = parse_qs(parts.query)
    assert query["process"] == ["login"]
    assert query["next"] == [f"https://{host_of(scenario.a)}/tables/x/"]
    main = Client().get("/accounts/login/").content.decode()
    assert "SSO de A" in main and "SSO de B" in main  # The main domain lists every provider, as before.
    assert "as organizações em que você tem vínculo" in main


def test_general_providers_stay_and_foreign_ones_go(scenario, monkeypatch):
    from types import SimpleNamespace

    from skeleton.accounts import providers
    from skeleton.web.templatetags.sites import site_providers

    bound = {"general": None, "mine": scenario.a.pk, "theirs": scenario.b.pk}
    monkeypatch.setattr(providers, "provider_organization_id", lambda provider_id: bound[provider_id])
    listed = [SimpleNamespace(id=name) for name in bound]
    site = SimpleNamespace(organization_id=scenario.a.pk)
    assert [item.id for item in site_providers({"organization_site": site}, listed)] == ["general", "mine"]
    assert site_providers({"organization_site": None}, listed) == listed


def test_without_shared_session_no_sso_button_on_the_host(scenario, settings):
    settings.SKELETON_PUBLIC_URL = "https://app.other.test"
    page = Client(HTTP_HOST=host_of(scenario.a)).get("/accounts/login/").content.decode()
    assert "SSO de A" not in page and 'name="password"' in page


def test_confirmation_on_the_main_domain_names_the_organization(scenario):
    url = f"/accounts/oidc/sso-a/login/?process=login&next=https://{host_of(scenario.a)}/tables/x/"
    page = Client().get(url).content.decode()
    assert "Continuar para Acme com SSO de A" in page
    assert '<form method="post" class="mt-6">' in page  # No action: posts back with the same query string.
    plain = Client().get("/accounts/oidc/sso-a/login/?process=login&next=https://evil.test/").content.decode()
    assert "Continuar para" not in plain


def test_absolute_next_back_to_the_organization_is_accepted_after_login(scenario):
    client = Client(enforce_csrf_checks=True, HTTP_HOST="app.example.test")
    target = f"https://{host_of(scenario.a)}/tables/x/"
    assert post_login(client, scenario.member, data={"next": target})["Location"] == target


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/",
        "https://route.example.test.evil.test/",
        "https://signin-a.example.test@evil.test/",
        "//evil.test/",
        "https://naoexiste.example.test/",
        "https://desligada.example.test/",
        "http://signin-a.example.test/",
        "https://www.example.test/",
        "https://signin-a.example.test:8443/",
        "https://signin-a.example.test\\@evil.test/",
    ],
)
def test_malicious_next_falls_back_to_the_default(scenario, target):
    client = Client(enforce_csrf_checks=True, HTTP_HOST="app.example.test")
    assert post_login(client, scenario.member, data={"next": target})["Location"] == "/"


def test_disabled_site_is_not_a_safe_next(scenario):
    from tests.site_support import disable_site

    disable_site(scenario, scenario.a, revision=1)
    client = Client(enforce_csrf_checks=True, HTTP_HOST="app.example.test")
    response = post_login(client, scenario.member, data={"next": f"https://{host_of(scenario.a)}/"})
    assert response["Location"] == "/"


def test_logout_on_the_host_returns_to_its_login(scenario):
    client = Client(enforce_csrf_checks=True, HTTP_HOST=host_of(scenario.a))
    post_login(client, scenario.member)
    response = client.post("/accounts/logout/", {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert response["Location"] == "/accounts/login/"
    assert client.get("/").status_code == 302


def test_account_email_on_the_host_greets_with_the_organization(scenario, mailoutbox):
    from skeleton.testing.jobs import run_pending_jobs

    client = Client(enforce_csrf_checks=True, HTTP_HOST=host_of(scenario.a))
    client.get("/accounts/password/reset/")
    client.post(
        "/accounts/password/reset/",
        {"email": scenario.member.email, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    run_pending_jobs()
    assert mailoutbox, "the reset email was sent"
    body = mailoutbox[-1].body
    assert "Acme" in body and host_of(scenario.a) in body


def test_totp_challenge_works_on_the_host(scenario):
    import time

    from allauth.mfa import app_settings
    from allauth.mfa.totp.internal.auth import TOTP, format_hotp_value, hotp_value

    secret = "JBSWY3DPEHPK3PXP"
    with override_settings(MFA_ENCRYPTION_KEYS=[Fernet.generate_key()]):
        with system_context(reason="test:site-mfa"):
            TOTP.activate(scenario.member, secret)
        client = Client(HTTP_HOST=host_of(scenario.a))
        response = client.post("/accounts/login/", {"login": scenario.member.email, "password": FIXTURE_PASSWORD})
        assert response.status_code == 302 and response["Location"].startswith("/accounts/2fa/")
        challenge = response["Location"]
        assert client.get(challenge).status_code == 200
        code = format_hotp_value(hotp_value(secret, int(time.time()) // app_settings.TOTP_PERIOD))
        assert client.post(challenge, {"code": code}).status_code == 302
        assert client.get("/").status_code == 200
