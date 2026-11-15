"""Database step of the setup: DSN from separate fields and classified connection failures (#464)."""

import logging
import socket

import pytest

from skeleton.setup import database, forms

SECRET = "p@ss:w/rd #?%x y"


def test_build_dsn_round_trips_every_character(subtests):
    for password in (SECRET, "simple", "ç~ã"):
        with subtests.test(password=password):
            dsn = database.build_dsn(host="db.example.test", port=6543, name="app db", user="run@x", password=password)
            config = database.parse(dsn)
            assert (config["PASSWORD"], config["USER"], config["NAME"]) == (password, "run@x", "app db")
            assert (config["HOST"], int(config["PORT"])) == ("db.example.test", 6543)


def test_build_dsn_brackets_ipv6_and_omits_an_empty_password():
    assert database.build_dsn(host="::1", port="5432", name="x", user="u", password="") == "postgres://u@[::1]:5432/x"
    assert database.parse(database.build_dsn(host="::1", port=5432, name="x", user="u", password="p"))["HOST"] == "::1"


def test_database_form_builds_the_dsn_or_takes_the_advanced_url():
    form = forms.DatabaseForm(
        {
            "db_host": "postgres",
            "db_port": "5432",
            "db_name": "app",
            "db_user": "skeleton_runtime",
            "db_password": SECRET,
        }
    )
    assert form.is_valid(), form.errors
    assert database.parse(form.cleaned_data["dsn"])["PASSWORD"] == SECRET
    advanced = forms.DatabaseForm({"database_url": "postgres://u:p@h:5432/d"})
    assert advanced.is_valid() and advanced.cleaned_data["dsn"] == "postgres://u:p@h:5432/d"
    missing = forms.DatabaseForm({"db_host": "postgres"})
    assert not missing.is_valid()
    assert set(missing.errors) == {"db_name", "db_user", "db_password"}
    assert missing.errors["db_name"] == ["Informe este campo."]


def test_schema_form_needs_user_and_password_unless_the_url_is_given():
    assert set(forms.SchemaForm({}).errors) == {"owner_user", "owner_password"}
    assert forms.SchemaForm({"owner_user": "skeleton_owner", "owner_password": "x"}).is_valid()
    assert forms.SchemaForm({"owner_url": "postgres://o:p@h/d"}).is_valid()


@pytest.fixture
def no_network(monkeypatch):
    """Name resolution and TCP succeed unless a test replaces them."""

    class Probe:
        def close(self):
            pass

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [()])
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Probe())


def _raise(error):
    def fail(*args, **kwargs):
        raise error

    return fail


DSN = database.build_dsn(host="db.example.test", port=5432, name="app", user="run-user", password=SECRET)


@pytest.mark.parametrize(
    ("target", "error", "code", "field"),
    [
        ("getaddrinfo", socket.gaierror(-2, "Name or service not known"), "dns", "db_host"),
        ("create_connection", ConnectionRefusedError(), "refused", "db_port"),
        ("create_connection", TimeoutError(), "timeout", "db_host"),
        ("create_connection", OSError("unreachable"), "network", "db_host"),
    ],
)
def test_network_failures_are_classified(no_network, monkeypatch, caplog, target, error, code, field):
    monkeypatch.setattr(socket, target, _raise(error))
    with caplog.at_level(logging.WARNING, logger="skeleton.setup"), pytest.raises(database.DatabaseCheckError) as info:
        database.inspect(DSN)
    assert (info.value.code, info.value.field) == (code, field)
    assert "db.example.test" in str(info.value)
    for leaked in (SECRET, "run-user"):
        assert leaked not in str(info.value) and leaked not in caplog.text
    assert f"code={code}" in caplog.text


@pytest.mark.parametrize(
    ("text", "sqlstate", "code", "field"),
    [
        ("FATAL: x", "28P01", "auth", "db_password"),
        ("FATAL: x", "28000", "auth", "db_password"),
        ('FATAL:  password authentication failed for user "run-user"', None, "auth", "db_password"),
        ('FATAL:  role "run-user" does not exist', None, "auth", "db_password"),
        ("FATAL: x", "3D000", "database_missing", "db_name"),
        ('FATAL:  database "app" does not exist', None, "database_missing", "db_name"),
        ("FATAL: x", "53300", "too_many_connections", None),
        ("FATAL:  sorry, too many connections already", None, "too_many_connections", None),
        ("server closed the connection unexpectedly", None, "connect_failed", None),
    ],
)
def test_driver_failures_are_classified_without_the_driver_text(
    no_network, monkeypatch, caplog, text, sqlstate, code, field
):
    import psycopg

    class Failure(psycopg.OperationalError):
        pass

    failure = Failure(text)
    monkeypatch.setattr(Failure, "sqlstate", sqlstate, raising=False)
    monkeypatch.setattr(psycopg, "connect", _raise(failure))
    with caplog.at_level(logging.WARNING, logger="skeleton.setup"), pytest.raises(database.DatabaseCheckError) as info:
        database.inspect(DSN)
    assert (info.value.code, info.value.field) == (code, field)
    for leaked in (text, SECRET, "run-user"):
        assert leaked not in str(info.value) and leaked not in caplog.text


def test_unix_socket_hosts_skip_the_network_probe(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _raise(AssertionError("probed")))
    assert database._network_problem("/var/run/postgresql", 5432) is None
    assert database._network_problem("", 5432) is None


def test_privilege_refusals_name_the_field(monkeypatch, subtests):
    base = {
        "name": "skeleton_runtime",
        "database": "app",
        "host": "h",
        "port": "5432",
        "superuser": False,
        "bypass_rls": False,
        "owns_schema": False,
        "schema_create": False,
        "schema_exists": True,
    }
    for changes, code, field in (
        ({"schema_exists": False}, "schema_missing", None),
        ({"superuser": True}, "superuser", "db_user"),
        ({"bypass_rls": True}, "bypassrls", "db_user"),
        ({"schema_create": True}, "owns_schema", "db_user"),
    ):
        with subtests.test(code=code):
            monkeypatch.setattr(database, "inspect", lambda dsn, changes=changes: database.Role(**{**base, **changes}))
            with pytest.raises(database.DatabaseCheckError) as info:
                database.validate_runtime("postgres://x@h/app")
            assert (info.value.code, info.value.field) == (code, field)
    runtime = database.Role(**base)
    for changes, code, field in (
        ({"database": "other"}, "owner_other_database", None),
        ({}, "owner_is_runtime", "owner_user"),
        ({"name": "skeleton_owner"}, "owner_not_owner", "owner_user"),
        ({"name": "postgres", "owns_schema": True, "superuser": True}, "owner_superuser", "owner_user"),
    ):
        with subtests.test(code=code):
            monkeypatch.setattr(database, "inspect", lambda dsn, changes=changes: database.Role(**{**base, **changes}))
            with pytest.raises(database.DatabaseCheckError) as info:
                database.validate_owner("postgres://x@h/app", runtime)
            assert (info.value.code, info.value.field) == (code, field)
