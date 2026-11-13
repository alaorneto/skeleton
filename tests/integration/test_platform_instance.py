"""Instance configuration at /platform/instance/ (#147): access, read-only env keys, no secrets.

The page writes to the instance SQLite only (no data table); it runs with the PostgreSQL
fixture because the platform guard and the login read identities from the database.
"""

import logging

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.instance.store import InstanceStore
from skeleton.instance.writer import write_setting

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Instance-page-Password-2026!"
SECRET = "very-private-smtp-password"


@pytest.fixture
def store(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(tmp_path / "instance.sqlite3"))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    monkeypatch.delenv("DEFAULT_FROM_EMAIL", raising=False)
    monkeypatch.delenv("EMAIL_URL", raising=False)
    write_setting(settings.BASE_DIR, "email_url", f"smtp+tls://user:{SECRET}@smtp.example.test:587", source="test")
    return InstanceStore(tmp_path / "instance.sqlite3")


def login(email, password=PASSWORD):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": email, "password": password, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


@pytest.fixture
def platform():
    user, _ = bootstrap_identity(
        email="instance-platform@example.test", organization="instance-platform", name="Platform", password=PASSWORD
    )
    return user, login(user.email)


def change(client, key, value, action="set"):
    return client.post(
        reverse("platform:instance-setting"),
        {"key": key, "value": value, "action": action, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )


def test_only_the_platform_reaches_the_page(store, platform):
    from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

    assert Client().get(reverse("platform:instance")).status_code == 302  # Anonymous: login.
    from django.apps import apps

    if apps.is_installed("examples.notes"):
        from examples.notes.models import Note

        scenario = build_service_scenario(Note, slug="instance-page")
        member = login(scenario.admin_a.email, FIXTURE_PASSWORD)
        assert member.get(reverse("platform:instance")).status_code == 403
        assert change(member, "default_from_email", "x@example.test").status_code == 403
    _, client = platform
    assert client.get(reverse("platform:instance")).status_code == 200


def test_secrets_never_leave_and_changes_are_logged_without_values(store, platform, caplog):
    user, client = platform
    with caplog.at_level(logging.INFO, logger="skeleton.security"):
        html = client.get(reverse("platform:instance")).content.decode()
        assert "email_url" in html and SECRET not in html and "••••" in html
        assert change(client, "default_from_email", "ops@example.test\nBcc: x@example.test").status_code == 400
        assert change(client, "default_from_email", "ops@example.test").status_code == 302
        assert change(client, "email_url", "").status_code == 400  # A secret needs a new value.
        assert change(client, "email_url", f"smtp+tls://user:{SECRET}-2@smtp.example.test:587").status_code == 302
    assert store.settings()["default_from_email"][0] == "ops@example.test"
    log = store.changes()
    assert ("default_from_email", "create", f"platform:{user.pk}") in [entry[:3] for entry in log]
    assert SECRET not in repr(log) and SECRET not in caplog.text
    assert SECRET not in store.path.read_bytes().decode("latin-1")
    events = [(getattr(record, "event", None), getattr(record, "reason_code", None)) for record in caplog.records]
    assert ("instance.setting_changed", "set:email_url") in events
    assert change(client, "default_from_email", "", action="clear").status_code == 302
    assert "default_from_email" not in store.settings()


def test_environment_keys_are_read_only(store, platform, monkeypatch):
    _, client = platform
    monkeypatch.setenv("DEFAULT_FROM_EMAIL", "env@example.test")
    html = client.get(reverse("platform:instance")).content.decode()
    assert "Somente leitura (ambiente)" in html
    assert change(client, "default_from_email", "ops@example.test").status_code == 409
    assert "default_from_email" not in store.settings()


def test_changes_need_a_recent_authentication(store, platform):
    _, client = platform
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = change(client, "default_from_email", "ops@example.test")
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    assert "default_from_email" not in store.settings()
    restart = client.post(
        reverse("platform:instance-restart"), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    )
    assert restart["Location"].startswith(reverse("account_reauthenticate"))


REQUIRED = {
    "secret_key": ("DJANGO_SECRET_KEY", "a-long-secret-key-only-for-this-test-2026-abcdefghijk"),
    "allowed_hosts": ("DJANGO_ALLOWED_HOSTS", "instance.example.test"),
    "database_url": ("DATABASE_URL", "postgres://runtime:pw@db.example.test:5432/skeleton"),
}


def test_required_keys_cannot_be_cleared(store, platform, monkeypatch, settings, caplog):
    """Clearing a key the instance needs to start would lock the site in setup mode (#267)."""
    _, client = platform
    for key, (_env, value) in REQUIRED.items():
        write_setting(settings.BASE_DIR, key, value, source="test")
    for env, _ in REQUIRED.values():
        monkeypatch.delenv(env, raising=False)
    with caplog.at_level(logging.INFO, logger="skeleton.security"):
        for key in REQUIRED:
            assert change(client, key, "", action="clear").status_code == 409, key
    assert set(REQUIRED) <= set(store.settings())
    events = [getattr(record, "reason_code", "") or "" for record in caplog.records]
    assert not [code for code in events if code.startswith("clear:")]


def test_clear_button_only_for_optional_keys(store, platform, monkeypatch, settings):
    _, client = platform
    monkeypatch.delenv("DJANGO_ALLOWED_HOSTS", raising=False)
    write_setting(settings.BASE_DIR, "allowed_hosts", "instance.example.test", source="test")
    write_setting(settings.BASE_DIR, "default_from_email", "ops@example.test", source="test")
    html = client.get(reverse("platform:instance")).content.decode()
    rows = {row.split("</span>", 1)[0]: row for row in html.split('<span class="font-medium text-heading">')[1:]}
    assert 'value="clear"' not in rows["allowed_hosts"]
    assert 'value="clear"' in rows["default_from_email"] and 'value="clear"' in rows["email_url"]
    assert html.count('value="clear"') == 2  # The two optional keys stored: email_url and default_from_email.


def test_invalid_keyring_is_refused_with_400(store, platform, caplog):
    """A keyring item that is not a Fernet key never reaches the store (#323)."""
    _, client = platform
    refused = "not-a-fernet-key-but-secret-2026"
    with caplog.at_level(logging.INFO):
        response = change(client, "integration_encryption_keys", refused)
    assert response.status_code == 400
    assert refused not in response.content.decode() and refused not in caplog.text
    assert "integration_encryption_keys" not in store.settings()
    assert refused not in repr(store.changes())
