"""First-access setup end to end with the Django client, against the test PostgreSQL (#146).

The runtime DSN is ``TEST_DATABASE_URL`` (skeleton_runtime) and the transient owner is
``TEST_MIGRATION_DATABASE_URL`` (skeleton_owner), as provisioned by deploy/postgres/bootstrap.sql.
"""

import logging
import os

import pytest
from django.core import mail, signing
from django.test import Client, override_settings
from django.urls import reverse

from skeleton.conf.profiles import setup_settings
from skeleton.instance.store import InstanceStore
from skeleton.setup import tokens

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Setup-admin-Password-2026!"


@pytest.fixture
def setup_mode(tmp_path, monkeypatch, settings):
    from django.conf import settings as django_settings

    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(tmp_path / "instance" / "instance.sqlite3"))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    import environ

    profile = setup_settings({"env": environ.Env(), "BASE_DIR": django_settings.BASE_DIR})
    overrides = {
        name: profile[name]
        for name in (
            "ROOT_URLCONF",
            "MIDDLEWARE",
            "SESSION_ENGINE",
            "SESSION_COOKIE_SAMESITE",
            "MESSAGE_STORAGE",
            "SECRET_KEY",
            "SKELETON_SETUP_MODE",
        )
    }
    overrides["SESSION_COOKIE_SECURE"] = False
    overrides["CSRF_COOKIE_SECURE"] = False
    with override_settings(**overrides):
        yield InstanceStore(tmp_path / "instance" / "instance.sqlite3")


def token(store):
    return tokens.token_file(store).read_text().strip()


def enter(store):
    client = Client(enforce_csrf_checks=True)
    client.get("/setup/")
    response = client.post("/setup/", {"token": token(store), "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert response.status_code == 302
    return client


def post(client, url, data):
    return client.post(url, {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def test_every_route_leads_to_the_setup_and_the_token_is_single_use(setup_mode):
    store = setup_mode
    anonymous = Client(enforce_csrf_checks=True)
    assert anonymous.get("/o/whatever/")["Location"] == "/setup/"
    assert anonymous.get("/setup/database/")["Location"] == reverse("setup:start")
    assert anonymous.get("/health/live/").status_code == 200
    anonymous.get("/setup/")
    csrf = anonymous.cookies["csrftoken"].value
    assert anonymous.post("/setup/", {"token": "x" * 32, "csrfmiddlewaretoken": csrf}).status_code == 403
    assert Client(enforce_csrf_checks=True).post("/setup/", {"token": token(store)}).status_code == 403  # CSRF.
    value = token(store)
    client = enter(store)
    assert client.get("/setup/").status_code == 200
    again = Client(enforce_csrf_checks=True)
    again.get("/setup/")
    reused = again.post("/setup/", {"token": value, "csrfmiddlewaretoken": again.cookies["csrftoken"].value})
    assert reused.status_code == 403


def test_repeated_wrong_tokens_are_rate_limited(setup_mode):
    client = Client(enforce_csrf_checks=True)
    client.get("/setup/")
    csrf = client.cookies["csrftoken"].value
    statuses = [client.post("/setup/", {"token": "y" * 32, "csrfmiddlewaretoken": csrf}).status_code for _ in range(6)]
    assert statuses[:5] == [403] * 5 and statuses[5] == 429


def test_privileged_runtime_dsn_is_refused_with_a_clear_message(setup_mode):
    client = enter(setup_mode)
    response = post(client, "/setup/database/", {"database_url": os.environ["TEST_MIGRATION_DATABASE_URL"]})
    assert response.status_code == 400
    assert "BYPASSRLS" in response.content.decode()
    assert "database_url" not in setup_mode.settings()


def test_full_setup_never_keeps_the_owner_credential(setup_mode, caplog, tmp_path):
    store = setup_mode
    owner = os.environ["TEST_MIGRATION_DATABASE_URL"]
    owner_password = owner.split(":")[2].split("@")[0]
    client = enter(store)
    with caplog.at_level(logging.DEBUG):
        assert post(
            client,
            "/setup/instance/",
            {"allowed_hosts": "app.example.test,testserver", "public_url": "https://app.example.test"},
        )["Location"] == reverse("setup:database")
        assert post(client, "/setup/database/", {"database_url": os.environ["TEST_DATABASE_URL"]}).status_code == 302
        refused = post(client, "/setup/schema/", {"owner_url": os.environ["TEST_DATABASE_URL"]})
        assert refused.status_code == 400  # The runtime role is not a migration owner.
        assert post(client, "/setup/schema/", {"owner_url": owner})["Location"] == reverse("setup:email")
        assert (
            post(
                client,
                "/setup/email/",
                {
                    "email_url": "memorymail://",
                    "default_from_email": "noreply@example.test",
                    "test_recipient": "ops@example.test",
                },
            ).status_code
            == 302
        )
        assert mail.outbox[-1].to == ["ops@example.test"]
        assert (
            post(
                client, "/setup/storage/", {"files_backend": "local", "files_root": str(tmp_path / "files")}
            ).status_code
            == 302
        )
        weak = post(
            client,
            "/setup/admin/",
            {
                "email": "root@example.test",
                "password1": "123",
                "password2": "123",
                "organization_name": "Primeira",
                "organization_slug": "primeira",
            },
        )
        assert weak.status_code == 400
        created = post(
            client,
            "/setup/admin/",
            {
                "email": "root@example.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
                "organization_name": "Primeira",
                "organization_slug": "primeira",
            },
        )
        assert created.get("Location") == reverse("setup:finish"), created.content.decode()[-1500:]
        # The owner credential is nowhere: not in the store, the logs or the signed session.
        assert owner_password not in store.path.read_bytes().decode("latin-1")
        assert owner_password not in caplog.text
        session = signing.loads(
            client.cookies["sessionid"].value, salt="django.contrib.sessions.backends.signed_cookies"
        )
        assert owner_password not in repr(session)
        stored = store.settings()
        assert stored["database_url"][1] and os.environ["TEST_DATABASE_URL"] not in stored["database_url"][0]
        assert {"secret_key", "mfa_encryption_keys", "integration_encryption_keys"} <= set(stored)
        done = post(client, "/setup/finish/", {})
    assert done.status_code == 200 and "reinicie o serviço" in done.content.decode().lower()
    assert store.setup_state()["status"] == "completed"
    assert Client().get("/setup/").status_code == 404
    from skeleton.accounts.models import User

    assert User.objects.filter(email="root@example.test", is_superuser=True).exists()


def test_correct_token_enters_after_wrong_attempts(setup_mode):
    """Wrong guesses from the same address never lock out the operator who has the token (#290)."""
    client = Client(enforce_csrf_checks=True)
    client.get("/setup/")
    csrf = client.cookies["csrftoken"].value
    for _ in range(6):
        client.post("/setup/", {"token": "z" * 32, "csrfmiddlewaretoken": csrf})
    response = client.post("/setup/", {"token": token(setup_mode), "csrfmiddlewaretoken": csrf})
    assert response.status_code == 302
    assert client.get("/setup/").status_code == 200


def test_rejected_tokens_are_logged_without_the_token(setup_mode, caplog):
    client = Client(enforce_csrf_checks=True)
    client.get("/setup/")
    csrf = client.cookies["csrftoken"].value
    guess = "w" * 31 + "q"
    with caplog.at_level(logging.WARNING, logger="skeleton.security"):
        for _ in range(6):
            client.post("/setup/", {"token": guess, "csrfmiddlewaretoken": csrf})
    rejected = [record for record in caplog.records if getattr(record, "event", None) == "setup.token_rejected"]
    assert [record.reason_code for record in rejected] == ["invalid_token"] * 5 + ["rate_limited"]
    assert guess not in caplog.text


def _ready_for(store, *steps):
    from django.conf import settings

    from skeleton.instance.writer import write_setting

    write_setting(settings.BASE_DIR, "database_url", os.environ["TEST_DATABASE_URL"], source="test")
    for step in steps:
        store.put_setup_state(f"step:{step}", "done")


@pytest.fixture
def dummy_default():
    """The default connection as a real setup-mode process has it: Django's dummy backend.

    Setup mode runs with ``DATABASES = {}``; the test settings keep a real default, which hid
    every read the owner and identity steps made through it (#451).
    """
    from django.db import connections
    from django.db.utils import ConnectionHandler, load_backend

    dummy = ConnectionHandler({}).settings["default"]
    original = connections["default"]
    original.close()
    connections["default"] = load_backend(dummy["ENGINE"]).DatabaseWrapper(dummy, "default")
    try:
        yield
    finally:
        connections["default"] = original


def _current_user():
    from django.db import connections

    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT current_user")
        return cursor.fetchone()[0]


def test_owner_and_identity_steps_work_without_a_real_default(dummy_default):
    """The schema step migrates and syncs the catalog through ``default``, then gives it back (#451)."""
    from django.core.exceptions import ImproperlyConfigured
    from django.db import connections

    from skeleton.setup import database
    from skeleton.system.models import TableDefinition

    owner = os.environ["TEST_MIGRATION_DATABASE_URL"]
    runtime = os.environ["TEST_DATABASE_URL"]
    shared = dict(connections.databases["default"])
    assert database.migrate_schema(owner) == []
    with pytest.raises(ImproperlyConfigured):
        connections["default"].cursor()
    with database.django_alias("default", owner):
        # One catalog, under the alias every other migrate writes: no rows for a setup alias.
        assert not TableDefinition.all_objects.exclude(database_alias="default").exists()
    with database.django_alias("default", runtime):
        assert _current_user() == database.parse(runtime)["USER"]
    with pytest.raises(ImproperlyConfigured):
        connections["default"].cursor()
    # Only this thread's connection object was swapped, never the shared DATABASES entry.
    assert connections.databases["default"] == shared


def test_default_connection_comes_back_when_the_step_fails(dummy_default):
    from django.core.exceptions import ImproperlyConfigured
    from django.db import connections

    from skeleton.setup import database

    with pytest.raises(RuntimeError), database.django_alias("default", os.environ["TEST_MIGRATION_DATABASE_URL"]):
        assert _current_user() == database.parse(os.environ["TEST_MIGRATION_DATABASE_URL"])["USER"]
        raise RuntimeError("step failed")
    with pytest.raises(ImproperlyConfigured):
        connections["default"].cursor()


def test_failed_migration_is_logged_without_credentials(setup_mode, caplog, monkeypatch):
    from skeleton.setup import database

    def broken(dsn):
        raise RuntimeError("boom")

    owner = os.environ["TEST_MIGRATION_DATABASE_URL"]
    owner_password = owner.split(":")[2].split("@")[0]
    _ready_for(setup_mode)
    monkeypatch.setattr(database, "migrate_schema", broken)
    client = enter(setup_mode)
    with caplog.at_level(logging.ERROR, logger="skeleton.setup"):
        response = post(client, "/setup/schema/", {"owner_url": owner})
    assert response.status_code == 400 and "RuntimeError" in response.content.decode()
    [record] = [record for record in caplog.records if record.name == "skeleton.setup"]
    assert record.levelno == logging.ERROR and record.exc_info and "step=schema" in record.getMessage()
    assert owner_password not in caplog.text and owner not in caplog.text


def test_failed_admin_bootstrap_is_logged(setup_mode, caplog, monkeypatch):
    from skeleton.accounts import services

    def broken(**kwargs):
        raise RuntimeError("boom")

    _ready_for(setup_mode, "schema")
    monkeypatch.setattr(services, "bootstrap_identity", broken)
    client = enter(setup_mode)
    with caplog.at_level(logging.ERROR, logger="skeleton.setup"):
        response = post(
            client,
            "/setup/admin/",
            {
                "email": "root@example.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
                "organization_name": "Primeira",
                "organization_slug": "primeira",
            },
        )
    assert response.status_code == 400
    [record] = [record for record in caplog.records if record.name == "skeleton.setup"]
    assert record.exc_info and "step=admin" in record.getMessage()
    assert PASSWORD not in caplog.text
