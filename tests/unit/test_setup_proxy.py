"""Setup mode behind a TLS-terminating proxy, without a database (#442).

The browser sends ``Origin: https://<host>`` while the proxy talks http to the backend; only an
explicit ``SKELETON_SETUP_TRUST_PROXY`` makes the setup believe ``X-Forwarded-Proto``.
"""

import environ
import pytest
from django.conf import settings as django_settings
from django.test import Client, override_settings

from skeleton.conf.profiles import setup_settings
from skeleton.instance.store import InstanceStore
from skeleton.setup import tokens

HOST = "setup.example.test"
NAMES = (
    "ROOT_URLCONF",
    "MIDDLEWARE",
    "SESSION_ENGINE",
    "SESSION_COOKIE_SAMESITE",
    "MESSAGE_STORAGE",
    "SECRET_KEY",
    "SKELETON_SETUP_MODE",
    "CSRF_TRUSTED_ORIGINS",
)


@pytest.fixture
def setup_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(tmp_path / "instance" / "instance.sqlite3"))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    monkeypatch.setenv("SKELETON_SETUP_ALLOWED_HOSTS", HOST)
    for name in ("SKELETON_SETUP_TRUST_PROXY", "DJANGO_TRUST_PROXY", "DJANGO_CSRF_TRUSTED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    store = InstanceStore(tmp_path / "instance" / "instance.sqlite3")

    def build(**environment):
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        profile = setup_settings({"env": environ.Env(), "BASE_DIR": django_settings.BASE_DIR})
        overrides = {name: profile[name] for name in NAMES}
        overrides["ALLOWED_HOSTS"] = profile["ALLOWED_HOSTS"]
        overrides["SECURE_PROXY_SSL_HEADER"] = profile.get("SECURE_PROXY_SSL_HEADER")
        # The test client keeps secure cookies over http; the flag itself is covered elsewhere.
        return override_settings(**overrides, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)

    return store, build


def post_token(store):
    """The token POST as the proxy forwards it: http to the backend, https in the browser."""
    client = Client(enforce_csrf_checks=True, HTTP_HOST=HOST, HTTP_X_FORWARDED_PROTO="https")
    page = client.get("/setup/")
    assert page.status_code == 200
    data = {
        "token": tokens.token_file(store).read_text().strip(),
        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
    }
    response = client.post("/setup/", data, HTTP_ORIGIN=f"https://{HOST}")
    return page, response


@pytest.mark.parametrize("variable", ["SKELETON_SETUP_TRUST_PROXY", "DJANGO_TRUST_PROXY"])
def test_trusted_proxy_lets_the_https_origin_through(setup_profile, variable):
    store, build = setup_profile
    with build(**{variable: "true"}):
        page, response = post_token(store)
        assert page.wsgi_request.is_secure()
    assert response.status_code == 302 and response["Location"] == "/setup/"


def test_forwarded_proto_is_ignored_by_default(setup_profile):
    store, build = setup_profile
    with build():
        page, response = post_token(store)
        assert not page.wsgi_request.is_secure()
    assert response.status_code == 403
    assert b"CSRF" in response.content
    assert tokens.token_file(store).exists()  # The token was never consumed.


def test_setup_variable_overrides_the_instance_trust(setup_profile):
    store, build = setup_profile
    with build(DJANGO_TRUST_PROXY="true", SKELETON_SETUP_TRUST_PROXY="false"):
        _, response = post_token(store)
    assert response.status_code == 403
