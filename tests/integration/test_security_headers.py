"""Every HTML response of the core carries a same-origin Content-Security-Policy (#266)."""

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings

pytestmark = pytest.mark.usefixtures("skeleton_db")


def header_name():
    return "Content-Security-Policy" if settings.SECURE_CSP else "Content-Security-Policy-Report-Only"


def directives(value):
    return {part.split()[0]: part.split()[1:] for part in (item.strip() for item in value.split(";")) if part}


def test_login_page_carries_the_same_origin_policy():
    response = Client().get("/accounts/login/")
    assert response.status_code == 200
    policy = directives(response[header_name()])
    assert policy["script-src"] == ["'self'"]
    assert policy["object-src"] == ["'none'"] and policy["frame-ancestors"] == ["'none'"]
    assert "'unsafe-inline'" not in response[header_name()] and "'unsafe-eval'" not in response[header_name()]
    assert "form-action" not in policy  # SSO starts with a POST that redirects to the identity provider.


def test_authenticated_pages_and_api_carry_the_policy(subtests):
    from skeleton.accounts.services import bootstrap_identity
    from tests.support import login

    password = "Disposable-csp-password-47!"
    user, organization = bootstrap_identity(
        email="csp-admin@example.test", organization="csp-a", name="CSP A", password=password
    )
    client = login(user, password=password)
    for path in ("/", f"/o/{organization.pk}/", "/platform/", "/api/v1/me/"):
        with subtests.test(path):
            response = client.get(path)
            assert response.status_code == 200
            policy = directives(response[header_name()])
            assert policy["script-src"] == ["'self'"] and policy["object-src"] == ["'none'"]


def test_brand_style_nonce_matches_the_header():
    """The organization's inline brand palette is allowed by a per-response nonce."""
    import re

    from django.middleware.csp import ContentSecurityPolicyMiddleware
    from django.template import RequestContext, Template

    def view(request):
        return HttpResponse(Template('<style nonce="{{ csp_nonce }}">x</style>').render(RequestContext(request)))

    responses = [ContentSecurityPolicyMiddleware(view)(RequestFactory().get("/o/")) for _ in range(2)]
    nonces = []
    for response in responses:
        rendered = re.search(r'nonce="([^"]+)"', response.content.decode()).group(1)
        assert f"'nonce-{rendered}'" in directives(response[header_name()])["style-src"]
        nonces.append(rendered)
    assert nonces[0] != nonces[1]


def test_attachment_downloads_keep_their_sandbox_policy():
    from django.middleware.csp import ContentSecurityPolicyMiddleware

    from skeleton.common.responses import protect

    middleware = ContentSecurityPolicyMiddleware(lambda request: protect(HttpResponse(b"x"), filename="a.pdf"))
    response = middleware(RequestFactory().get("/download"))
    assert response["Content-Security-Policy"] == "sandbox"


def test_setup_pages_carry_the_policy(tmp_path, monkeypatch):
    import environ
    from django.conf import settings as django_settings

    from skeleton.conf.profiles import setup_settings

    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(tmp_path / "instance" / "instance.sqlite3"))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    profile = setup_settings({"env": environ.Env(), "BASE_DIR": django_settings.BASE_DIR})
    names = (
        "ROOT_URLCONF",
        "MIDDLEWARE",
        "SESSION_ENGINE",
        "SESSION_COOKIE_SAMESITE",
        "MESSAGE_STORAGE",
        "SECRET_KEY",
        "SKELETON_SETUP_MODE",
    )
    with override_settings(
        **{name: profile[name] for name in names}, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False
    ):
        response = Client().get("/setup/")
    assert response.status_code == 200
    assert directives(response[header_name()])["script-src"] == ["'self'"]
