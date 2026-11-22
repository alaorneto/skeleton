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
            "SESSION_COOKIE_AGE",
            "SESSION_SAVE_EVERY_REQUEST",
            "MESSAGE_STORAGE",
            "SECRET_KEY",
            "SKELETON_SETUP_MODE",
        )
    }
    overrides["SESSION_COOKIE_SECURE"] = False
    overrides["CSRF_COOKIE_SECURE"] = False
    # The flow sends the test email to the locmem outbox and keeps the local cache, as the
    # runserver's development target allows; the production rules are in test_setup_forms (#457).
    overrides["SKELETON_SETUP_PRODUCTION"] = False
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


def test_full_setup_never_keeps_the_owner_credential(setup_mode, caplog, tmp_path, monkeypatch):
    from skeleton.setup import preflight

    # The start check runs the target settings in a child process; it has its own tests (#467).
    monkeypatch.setattr(preflight, "start_problems", lambda target, base_dir: [])
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
    # Until the process restarts, the setup only answers with the waiting page (#467).
    assert Client().get("/setup/").status_code == 503
    assert Client().post("/setup/database/", {"database_url": "postgres://x:y@h/d"}).status_code == 503
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
    _ready_for(setup_mode, "database")
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

    _ready_for(setup_mode, "database", "schema")
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


def test_steps_show_their_position_intro_and_a_way_back(setup_mode, subtests):
    _ready_for(setup_mode, "database", "schema")
    client = enter(setup_mode)
    for number, (url, back) in enumerate(
        (
            ("/setup/instance/", "/setup/"),
            ("/setup/database/", "/setup/instance/"),
            ("/setup/schema/", "/setup/database/"),
            ("/setup/email/", "/setup/schema/"),
            ("/setup/storage/", "/setup/email/"),
            ("/setup/admin/", "/setup/storage/"),
        ),
        1,
    ):
        with subtests.test(url=url):
            response = client.get(url)
            html = response.content.decode()
            assert response.status_code == 200
            assert f"Etapa {number} de 6" in html
            assert 'class="page-description"' in html
            assert f'<a class="btn btn-secondary" href="{back}">Voltar</a>' in html
            assert 'aria-current="step"' in html


def test_optional_and_required_fields_are_marked(setup_mode, subtests):
    _ready_for(setup_mode, "database", "schema")
    client = enter(setup_mode)
    for url, required, optional in (
        ("/setup/instance/", ("allowed_hosts",), ("public_url", "csrf_trusted_origins")),
        ("/setup/storage/", (), ("cache_location", "cache_password", "files_root", "files_s3_region")),
        ("/setup/admin/", ("email", "password1", "organization_slug"), ()),
    ):
        html = client.get(url).content.decode()
        for name in required:
            with subtests.test(url=url, required=name):
                label = html.split(f'<label for="id_{name}">', 1)[1].split("</label>", 1)[0]
                assert '<span class="required" aria-hidden="true"> *</span>' in label and "(opcional)" not in label
        for name in optional:
            with subtests.test(url=url, optional=name):
                label = html.split(f'<label for="id_{name}">', 1)[1].split("</label>", 1)[0]
                assert '<span class="optional">(opcional)</span>' in label and "*" not in label


def test_production_target_requires_the_redis_address(setup_mode, tmp_path):
    client = enter(setup_mode)
    data = {"files_backend": "local", "files_root": str(tmp_path / "files"), "cache_password": "redis-secret-x"}
    with override_settings(SKELETON_SETUP_PRODUCTION=True):
        html = client.get("/setup/storage/").content.decode()
        label = html.split('<label for="id_cache_location">', 1)[1].split("</label>", 1)[0]
        assert "required" in label and "Obrigatório em produção" in html
        refused = post(client, "/setup/storage/", data)
    assert refused.status_code == 400 and "cache_location" in refused.context["form"].errors
    assert "redis-secret-x" not in refused.content.decode()
    assert "cache_url" not in setup_mode.settings()
    with override_settings(SKELETON_SETUP_PRODUCTION=False):
        assert post(client, "/setup/storage/", data).status_code == 302
    assert "cache_url" not in setup_mode.settings()


def test_storage_step_stores_the_redis_url_with_the_separate_password(setup_mode, tmp_path):
    from urllib.parse import unquote, urlsplit

    from skeleton.setup.views import _stored

    client = enter(setup_mode)
    password = "p@ss:w/rd"
    data = {
        "cache_location": "redis://redis:6379/0",
        "cache_password": password,
        "files_backend": "local",
        "files_root": str(tmp_path / "files"),
    }
    with override_settings(SKELETON_SETUP_PRODUCTION=True):
        response = post(client, "/setup/storage/", data)
    assert response.status_code == 302
    assert unquote(urlsplit(_stored("cache_url")).password) == password
    assert password not in client.get("/setup/storage/").content.decode()


def test_instance_step_suggests_the_host_and_fills_the_csrf_origin(setup_mode):
    client = enter(setup_mode)
    html = client.get("/setup/instance/", HTTP_HOST="testserver:8000").content.decode()
    assert 'name="allowed_hosts" value="testserver"' in html
    assert "checked" not in html.split('name="trust_proxy"', 1)[1].split(">", 1)[0]
    proxied = client.get("/setup/instance/", HTTP_X_FORWARDED_PROTO="https").content.decode()
    assert "checked" in proxied.split('name="trust_proxy"', 1)[1].split(">", 1)[0]
    response = post(
        client, "/setup/instance/", {"allowed_hosts": "app.example.test", "public_url": "https://app.example.test"}
    )
    assert response.status_code == 302
    assert setup_mode.settings()["csrf_trusted_origins"][0] == "https://app.example.test"
    after = client.get(response["Location"]).content.decode()
    assert '<div role="status" class="message success">' in after and "Etapa “Instância” salva." in after


def test_overview_lists_what_to_have_and_leads_to_the_next_pending_step(setup_mode):
    client = enter(setup_mode)
    html = client.get("/setup/").content.decode()
    assert "Antes de começar, tenha em mãos" in html and "skeleton_runtime" in html
    assert '<a class="btn btn-primary" href="/setup/instance/">Começar</a>' in html
    setup_mode.put_setup_state("step:instance", "done")
    html = client.get("/setup/").content.decode()
    assert '<a class="btn btn-primary" href="/setup/database/">Continuar: Banco de dados</a>' in html
    for name in ("database", "schema", "email", "storage", "admin"):
        setup_mode.put_setup_state(f"step:{name}", "done")
    assert (
        '<a class="btn btn-primary" href="/setup/finish/">Revisar e concluir</a>'
        in client.get("/setup/").content.decode()
    )


def _fields(url, prefix):
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    if prefix == "owner_":
        return {"owner_user": unquote(parts.username), "owner_password": unquote(parts.password or "")}
    return {
        "db_host": parts.hostname,
        "db_port": str(parts.port or 5432),
        "db_name": parts.path.lstrip("/"),
        "db_user": unquote(parts.username),
        "db_password": unquote(parts.password or ""),
    }


def test_database_and_schema_steps_take_separate_fields(setup_mode, caplog, monkeypatch):
    from skeleton.setup import database

    runtime = os.environ["TEST_DATABASE_URL"]
    owner = os.environ["TEST_MIGRATION_DATABASE_URL"]
    monkeypatch.setattr(database, "migrate_schema", lambda dsn: [])
    client = enter(setup_mode)
    html = client.get("/setup/database/").content.decode()
    assert html.count('type="password"') == 2  # The role password and the advanced URL, in <details>.
    assert "Avançado: informar a URL completa" in html
    for name in ("db_host", "db_port", "db_name", "db_user"):
        assert f'name="{name}"' in html and f'type="password" name="{name}"' not in html
    with caplog.at_level(logging.DEBUG):
        saved = post(client, "/setup/database/", _fields(runtime, "db_"))
        assert saved.status_code == 302, saved.content.decode()[-2000:]
        page = client.get("/setup/schema/").content.decode()
        fields = _fields(runtime, "db_")
        assert f"<code>{fields['db_host']}:{fields['db_port']}</code>" in page and fields["db_name"] in page
        same = post(client, "/setup/schema/", _fields(runtime, "owner_"))
        assert same.status_code == 400
        assert "owner_user" in same.context["form"].errors and "role de aplicação" in same.content.decode()
        assert post(client, "/setup/schema/", _fields(owner, "owner_")).status_code == 302
    for secret in (_fields(runtime, "db_")["db_password"], _fields(owner, "owner_")["owner_password"]):
        assert secret not in caplog.text
    assert database.parse(_stored_url(setup_mode))["USER"] == _fields(runtime, "db_")["db_user"]


def _stored_url(store):
    from skeleton.setup.views import _stored

    return _stored("database_url")


def test_database_step_shows_where_the_connection_failed(setup_mode, caplog, subtests):
    runtime = _fields(os.environ["TEST_DATABASE_URL"], "db_")
    client = enter(setup_mode)
    for changes, field, text in (
        ({"db_host": "no-such-host.invalid"}, "db_host", "não foi encontrado"),
        ({"db_user": "no_such_role_464"}, "db_password", "recusou o usuário ou a senha"),
        ({"db_name": "no_such_database_464"}, "db_name", "O banco informado não existe"),
    ):
        with subtests.test(field=field), caplog.at_level(logging.DEBUG):
            response = post(client, "/setup/database/", {**runtime, **changes})
            html = response.content.decode()
            assert response.status_code == 400 and field in response.context["form"].errors
            assert text in str(response.context["form"].errors[field])
            assert runtime["db_password"] not in html and runtime["db_password"] not in caplog.text
            assert "FATAL" not in html and "FATAL" not in caplog.text
            # The typed user stays in its own input, never in a message.
            if "db_user" in changes:
                assert changes["db_user"] not in str(response.context["form"].errors) + caplog.text
    assert "database_url" not in setup_mode.settings()


SMTP_FIELDS = {
    "smtp_host": "smtp.example.test",
    "smtp_port": "587",
    "smtp_security": "starttls",
    "smtp_user": "mailer@example.test",
    "smtp_password": "smtp-secret-465",
    "default_from_email": "noreply@example.test",
    "test_recipient": "ops@example.test",
}


def test_email_step_can_wait_outside_production(setup_mode):
    client = enter(setup_mode)
    html = client.get("/setup/email/").content.decode()
    assert "Configurar depois" in html and "Neste modo você pode deixar para depois" in html
    assert "(opcional neste modo)" in client.get("/setup/").content.decode()
    response = post(client, "/setup/email/", {"email_mode": "later"})
    assert response["Location"] == reverse("setup:storage")
    assert "email_url" not in setup_mode.settings() and setup_mode.setup_state()["step:email"] == "skipped"
    after = client.get("/setup/storage/").content.decode()
    assert "E-mail ficou para depois" in after
    overview = client.get("/setup/").content.decode()
    assert '<span class="badge badge-warning">depois</span>' in overview
    for name in ("instance", "database", "schema", "storage", "admin"):
        setup_mode.put_setup_state(f"step:{name}", "done")
    finish = client.get("/setup/finish/").content.decode()
    assert "E-mail não configurado" in finish and "Conferir e concluir" in finish


def test_email_step_in_production_only_takes_smtp(setup_mode):
    client = enter(setup_mode)
    with override_settings(SKELETON_SETUP_PRODUCTION=True):
        html = client.get("/setup/email/").content.decode()
        assert "Configurar depois" not in html and "Em produção o servidor SMTP é obrigatório" in html
        refused = post(
            client,
            "/setup/email/",
            {"email_url": "consolemail://", "default_from_email": "a@b.test", "test_recipient": "c@d.test"},
        )
        later = post(client, "/setup/email/", {"email_mode": "later"})
    assert refused.status_code == 400 and "Em produção só um servidor SMTP é aceito." in refused.content.decode()
    assert later.status_code == 400 and "step:email" not in setup_mode.setup_state()


def test_email_test_failure_points_at_the_field(setup_mode, caplog, monkeypatch):
    import smtplib

    from skeleton.setup import views

    class Refusing:
        def send_messages(self, messages):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 mailer@example.test credentials rejected")

    monkeypatch.setattr(views, "_email_connection", lambda url: Refusing())
    client = enter(setup_mode)
    with caplog.at_level(logging.DEBUG):
        response = post(client, "/setup/email/", SMTP_FIELDS)
    html = response.content.decode()
    assert response.status_code == 400 and "smtp_password" in response.context["form"].errors
    for leaked in ("smtp-secret-465", "credentials rejected"):
        assert leaked not in html and leaked not in caplog.text
    assert "email_url" not in setup_mode.settings()


def test_email_step_sends_the_test_by_separate_fields(setup_mode, monkeypatch):
    from skeleton.setup import views

    sent, urls = [], []

    class Accepting:
        def send_messages(self, messages):
            sent.extend(messages)
            return len(messages)

    def connection(url):
        urls.append(url)
        return Accepting()

    monkeypatch.setattr(views, "_email_connection", connection)
    client = enter(setup_mode)
    response = post(client, "/setup/email/", SMTP_FIELDS)
    assert response["Location"] == reverse("setup:storage")
    assert urls == ["smtp+tls://mailer%40example.test:smtp-secret-465@smtp.example.test:587"]
    assert sent[0].to == ["ops@example.test"]
    assert "Mensagem de teste enviada para ops@example.test" in client.get("/setup/storage/").content.decode()


def test_saved_steps_come_back_filled_without_secrets(setup_mode, tmp_path, monkeypatch):
    from skeleton.setup import database

    runtime = _fields(os.environ["TEST_DATABASE_URL"], "db_")
    client = enter(setup_mode)
    post(client, "/setup/instance/", {"allowed_hosts": "app.example.test", "public_url": "https://app.example.test"})
    html = client.get("/setup/instance/").content.decode()
    assert 'value="app.example.test"' in html and 'value="https://app.example.test"' in html
    assert "Esta etapa já foi salva" in html and 'href="/setup/database/">Próxima etapa</a>' in html
    assert post(client, "/setup/database/", runtime).status_code == 302
    html = client.get("/setup/database/").content.decode()
    for name in ("db_host", "db_name", "db_user"):
        assert f'value="{runtime[name]}"' in html
    assert runtime["db_password"] not in html and "Já definida. Deixe em branco para manter a atual." in html
    assert f"Conectado como {runtime['db_user']} ao banco {runtime['db_name']}." in html
    # The same port again with a blank password keeps the stored one.
    assert post(client, "/setup/database/", {**runtime, "db_password": ""}).status_code == 302
    assert database.parse(_stored_url(setup_mode))["PASSWORD"] == runtime["db_password"]
    data = {
        "cache_location": "redis://redis:6379/0",
        "cache_password": "redis-secret-466",
        "files_backend": "local",
        "files_root": str(tmp_path / "files"),
    }
    assert post(client, "/setup/storage/", data).status_code == 302
    html = client.get("/setup/storage/").content.decode()
    assert 'value="redis://redis:6379/0"' in html and "redis-secret-466" not in html
    assert post(client, "/setup/storage/", {**data, "cache_password": ""}).status_code == 302
    from skeleton.setup.views import _secret_of

    assert _secret_of("cache_url") == "redis-secret-466"


def test_unreadable_stored_values_leave_the_form_blank(setup_mode, monkeypatch):
    from skeleton.setup import views

    client = enter(setup_mode)
    setup_mode.put_setup_state("step:instance", "done")

    def broken(key):
        raise ValueError("unreadable")

    monkeypatch.setattr(views, "_stored", broken)
    for url in ("/setup/instance/", "/setup/database/", "/setup/email/", "/setup/storage/"):
        assert client.get(url).status_code == 200


def test_every_setup_response_renews_the_session(setup_mode):
    client = enter(setup_mode)
    for _ in range(2):
        response = client.get("/setup/instance/")
        assert "sessionid" in response.cookies and response.cookies["sessionid"]["max-age"] == 2 * 3600


def test_expired_session_and_step_order_explain_the_redirect(setup_mode):
    anonymous = Client()
    response = anonymous.get("/setup/database/", follow=True)
    assert response.redirect_chain[-1][0] == reverse("setup:start")
    assert "Sua sessão do setup expirou" in response.content.decode()
    client = enter(setup_mode)
    response = client.get("/setup/admin/", follow=True)
    assert response.redirect_chain[-1][0] == reverse("setup:database")
    assert "Conclua antes a etapa “Banco de dados”." in response.content.decode()
    _ready_for(setup_mode, "database")
    response = client.get("/setup/admin/", follow=True)
    assert response.redirect_chain[-1][0] == reverse("setup:schema")
    assert "Conclua antes a etapa “Tabelas do banco”." in response.content.decode()


def test_refused_token_points_at_the_command(setup_mode):
    client = Client(enforce_csrf_checks=True)
    client.get("/setup/")
    response = client.post("/setup/", {"token": "x" * 32, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    html = response.content.decode()
    assert "Veja abaixo como gerar um novo." in html and "manage.py instance setup-token" in html


def _all_done(store):
    for name in ("instance", "database", "schema", "email", "storage", "admin"):
        store.put_setup_state(f"step:{name}", "done")


def test_instance_step_stores_the_default_timezone(setup_mode):
    client = enter(setup_mode)
    html = client.get("/setup/instance/").content.decode()
    assert '<option value="America/Sao_Paulo" selected>' in html
    response = post(
        client,
        "/setup/instance/",
        {"allowed_hosts": "app.example.test", "default_timezone": "America/Manaus"},
    )
    assert response.status_code == 302
    assert setup_mode.settings()["default_timezone"][0] == "America/Manaus"
    assert '<option value="America/Manaus" selected>' in client.get("/setup/instance/").content.decode()


def test_finish_waits_for_a_clean_start(setup_mode, monkeypatch):
    from skeleton.setup import preflight

    calls = []

    def problems(target, base_dir):
        calls.append(target)
        return ["A instância não subiria com esta configuração: Falta o Redis: preencha o endereço do Redis."]

    _all_done(setup_mode)
    monkeypatch.setattr(preflight, "start_problems", problems)
    client = enter(setup_mode)
    assert "Conferir e concluir" in client.get("/setup/finish/").content.decode()
    refused = post(client, "/setup/finish/", {})
    assert refused.status_code == 400 and "Falta o Redis" in refused.content.decode()
    assert setup_mode.setup_state().get("status") != "completed" and calls
    monkeypatch.setattr(preflight, "start_problems", lambda target, base_dir: [])
    done = post(client, "/setup/finish/", {})
    html = done.content.decode()
    assert done.status_code == 200 and setup_mode.setup_state()["status"] == "completed"
    assert "docker compose restart web worker scheduler" in html and "sudo systemctl restart" in html


def test_finish_completes_with_a_warning_when_no_child_process_starts(setup_mode, monkeypatch):
    from skeleton.setup import preflight

    def unavailable(target, base_dir):
        raise preflight.PreflightUnavailable

    _all_done(setup_mode)
    monkeypatch.setattr(preflight, "start_problems", unavailable)
    client = enter(setup_mode)
    assert post(client, "/setup/finish/", {}).status_code == 200
    assert setup_mode.setup_state()["status"] == "completed"


def test_a_completed_setup_only_shows_the_waiting_page(setup_mode, subtests):
    client = enter(setup_mode)
    setup_mode.put_setup_state("status", "completed")
    for method, url in (("get", "/"), ("get", "/setup/"), ("get", "/setup/finish/"), ("post", "/setup/database/")):
        with subtests.test(method=method, url=url):
            response = getattr(client, method)(url, {"database_url": "postgres://x:y@h/d"} if method == "post" else {})
            html = response.content.decode()
            assert response.status_code == 503 and response["Retry-After"] == "5"
            assert "O serviço está reiniciando" in html and '<meta http-equiv="refresh" content="5">' in html
            assert "<form" not in html and "csrfmiddlewaretoken" not in html
    assert "database_url" not in setup_mode.settings()
    assert client.get("/health/live/").status_code == 200


def test_admin_step_warns_that_the_password_does_not_change(setup_mode):
    _ready_for(setup_mode, "database", "schema", "admin")
    client = enter(setup_mode)
    html = client.get("/setup/admin/").content.decode()
    assert "O administrador já foi criado" in html and "não altera a senha" in html


def test_bootstrap_identity_messages_are_in_portuguese(subtests):
    from django.core.exceptions import ValidationError

    from skeleton.accounts.services import bootstrap_identity, create_identity

    create_identity(email="member-467@example.test", password=PASSWORD)
    with subtests.test("existing non-admin"), pytest.raises(ValidationError) as info:
        bootstrap_identity(email="member-467@example.test", organization="org-467", name="Org", password=PASSWORD)
    assert "Esta conta já existe e não é administradora da plataforma" in info.value.messages[0]
    with subtests.test("no password"), pytest.raises(ValidationError) as info:
        bootstrap_identity(email="new-467@example.test", organization="org-467", name="Org")
    assert info.value.messages == ["Informe uma senha para a nova conta."]


def test_agent_managed_schema_step_only_confirms_with_the_runtime_role(setup_mode, monkeypatch):
    """#652: the agent migrated already; the step never asks for the owner and checks as runtime."""
    from skeleton.setup import database

    monkeypatch.setenv("SKELETON_SETUP_SCHEMA", "agent")
    seen = []
    monkeypatch.setattr(database, "confirm_schema", lambda dsn: seen.append(dsn) or [])
    monkeypatch.setattr(database, "migrate_schema", lambda dsn: pytest.fail("the agent migrates, not the setup"))
    _ready_for(setup_mode, "instance", "database")
    client = enter(setup_mode)
    page = client.get("/setup/schema/").content.decode()
    assert "O agente de deployment já criou as tabelas" in page and 'name="owner_password"' not in page
    response = post(client, "/setup/schema/", {})
    assert response.status_code == 302 and response["Location"] == reverse("setup:email")
    assert seen == [os.environ["TEST_DATABASE_URL"]]
    monkeypatch.setattr(database, "confirm_schema", lambda dsn: ["tabela sem trigger"])
    refused = post(client, "/setup/schema/", {})
    assert refused.status_code == 400 and "tabela sem trigger" in refused.content.decode()


def test_confirm_schema_checks_the_deployed_schema_with_the_given_role(dummy_default):
    from skeleton.setup import database

    assert database.confirm_schema(os.environ["TEST_DATABASE_URL"]) == []
    assert database.agent_manages_schema({"SKELETON_SETUP_SCHEMA": "agent"})
    assert not database.agent_manages_schema({})
