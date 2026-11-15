"""Administrative maintenance mode (#446): instance state, cached reader, 503 answer, gate and CLI.

No test here opens PostgreSQL: the mode is decided before sessions and the database, and the
CLI must work with the database down (pytest-django refuses any database access in these tests).
"""

import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings

from skeleton.common.maintenance_response import maintenance_response
from skeleton.deployment import DeploymentInventory, MaintenanceMiddleware, maintenance_open_route
from skeleton.instance import maintenance
from skeleton.instance.maintenance import (
    INACTIVE,
    UNREADABLE,
    MaintenanceError,
    MaintenanceReader,
    MaintenanceState,
    clean_message,
    enter,
    leave,
    read_state,
)
from skeleton.instance.store import SCHEMA_VERSION, InstanceStore
from skeleton.observability.metrics import MAINTENANCE_ROUTE, route_label

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def store_file(tmp_path):
    path = tmp_path / "instance" / "instance.sqlite3"
    with override_settings(SKELETON_MAINTENANCE_STORE=str(path)):
        yield path


def _middleware(called, marker=None):
    with override_settings(SKELETON_MAINTENANCE_MARKER=str(marker) if marker else None):
        return MaintenanceMiddleware(lambda request: called.append(request) or HttpResponse("active"))


def test_store_upgrades_to_version_two_and_logs_enter_exit_without_the_message(tmp_path):
    path = tmp_path / "instance.sqlite3"
    with closing(sqlite3.connect(path)) as connection:  # A version 1 store, as written before #446.
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version (version) VALUES (1)")
        connection.commit()
    store = InstanceStore(path)
    assert store.maintenance() is None, "A store without the table has never been in maintenance."
    store.initialize()
    assert SCHEMA_VERSION == 2 and store.schema_version() == 2
    store.initialize()
    assert store.schema_version() == 2

    assert store.put_maintenance(active=True, message="segredo-nao", ends_at=None, pause_jobs=True, source="cli:x")
    first = store.maintenance()
    assert first["active"] and first["message"] == "segredo-nao" and first["pause_jobs"]
    store.put_maintenance(active=True, message="outra", ends_at=None, pause_jobs=False, source="cli:y")
    assert store.maintenance()["started_at"] == first["started_at"], "Updating keeps when it started."
    assert store.put_maintenance(active=False, message="", ends_at=None, pause_jobs=False, source="cli:z")
    assert not store.put_maintenance(active=False, message="", ends_at=None, pause_jobs=False, source="cli:z")
    row = store.maintenance()
    assert row["active"] is False and row["message"] == "" and row["started_at"] is None
    changes = [change for change in store.changes() if change[0] == "maintenance"]
    assert [(action, source) for _, action, source, _ in changes] == [
        ("enter", "cli:x"),
        ("enter", "cli:y"),
        ("exit", "cli:z"),
    ]
    assert all("segredo" not in "".join(change) for change in store.changes()), "The log never keeps the message."


def test_state_expires_on_read_and_computes_retry_after():
    state = MaintenanceState(active=True, ends_at=NOW + timedelta(minutes=10))
    assert state.is_active(NOW) and not state.expired(NOW)
    assert state.retry_after(NOW) == 600
    assert MaintenanceState(active=True, ends_at=NOW + timedelta(seconds=5)).retry_after(NOW) == 30
    assert not state.is_active(NOW + timedelta(minutes=10)) and state.expired(NOW + timedelta(minutes=10))
    assert MaintenanceState(active=True).is_active(NOW) and MaintenanceState(active=True).retry_after(NOW) == 300
    assert not INACTIVE.is_active(NOW) and UNREADABLE.is_active(NOW) and UNREADABLE.pause_jobs


def test_message_and_end_are_validated(tmp_path):
    assert clean_message("  Linha 1\r\nLinha 2  ") == "Linha 1\nLinha 2"
    assert clean_message(None) == ""
    for bad in ("x" * 501, "sino\x07", "rtl‮override", "nul\x00"):
        with pytest.raises(MaintenanceError):
            clean_message(bad)
    store = InstanceStore(tmp_path / "i.sqlite3")
    with pytest.raises(MaintenanceError, match="futuro"):
        enter(store, message="", ends_at=NOW - timedelta(seconds=1), pause_jobs=True, source="t", now=NOW)
    with pytest.raises(MaintenanceError, match="fuso"):
        enter(store, message="", ends_at=datetime(2030, 1, 1), pause_jobs=True, source="t", now=NOW)
    assert not store.exists(), "Invalid input writes nothing."
    assert leave(store, source="t") is False


def test_reader_follows_file_changes_and_fails_closed_only_before_a_good_read(tmp_path, caplog):
    path = tmp_path / "i.sqlite3"
    clock = [0.0]
    reader = MaintenanceReader(path, clock=lambda: clock[0])
    assert reader.state() is INACTIVE, "No store at all means no maintenance."
    store = InstanceStore(path)
    enter(store, message="Janela", ends_at=None, pause_jobs=False, source="t")
    state = reader.state()
    assert state.is_active() and state.message == "Janela", "A changed file is reread at once."
    assert reader.state() is state, "An unchanged file is served from the cache."
    leave(store, source="t")
    assert not reader.state().is_active()
    enter(store, message="De novo", ends_at=None, pause_jobs=False, source="t")
    good = reader.state()

    path.write_bytes(b"not a sqlite database" * 100)
    with caplog.at_level(logging.ERROR, logger="skeleton.instance.maintenance"):
        assert reader.state() is good, "A later failure keeps the last state read."
    assert any(record.msg == "maintenance.state_unreadable" for record in caplog.records)
    assert "De novo" not in caplog.text

    fresh = MaintenanceReader(path, clock=lambda: clock[0])
    assert fresh.state() is UNREADABLE, "Never read successfully: fail closed."


def test_response_is_json_for_the_api_and_standalone_html_otherwise():
    factory = RequestFactory()
    end = NOW + timedelta(hours=1)
    api = maintenance_response(factory.get("/api/v1/x/"), message="Volta já", ends_at=end, retry_after=3600)
    assert api.status_code == 503 and api["Retry-After"] == "3600" and api["Cache-Control"] == "no-store"
    assert json.loads(api.content) == {"code": "maintenance", "detail": "Volta já", "ends_at": end.isoformat()}
    accept = maintenance_response(
        factory.get("/x/", HTTP_ACCEPT="application/json"), message=None, ends_at=None, retry_after=300
    )
    assert json.loads(accept.content)["detail"] == "O sistema está em manutenção. Tente novamente mais tarde."
    assert json.loads(accept.content)["ends_at"] is None
    deploy = maintenance_response(
        factory.get("/api/v1/x/"), message=None, ends_at=None, retry_after=60, deployment=True
    )
    assert json.loads(deploy.content)["detail"].startswith("O sistema está sendo atualizado")

    page = maintenance_response(
        factory.get("/", HTTP_ACCEPT="text/html,*/*"),
        message="<script>alert(1)</script>",
        ends_at=end,
        retry_after=3600,
        login_url="/accounts/login/?next=%2Fplatform%2F",
    )
    html = page.content.decode()
    assert page["Content-Type"].startswith("text/html")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert '<html lang="pt-br">' in html
    assert "13/11/2026 13:00 UTC" in html and 'href="/accounts/login/?next=%2Fplatform%2F"' in html
    head = maintenance_response(factory.head("/"), message=None, ends_at=None, retry_after=300)
    assert head.status_code == 503 and head.content == b""


def test_deployment_marker_closes_everything_with_the_shared_answer(tmp_path, store_file):
    marker = tmp_path / "maintenance"
    marker.touch()
    called = []
    middleware = _middleware(called, marker)
    for path in ("/accounts/login/", "/platform/", "/api/v1/x/"):
        response = middleware(RequestFactory().get(path))
        assert response.status_code == 503 and response["Retry-After"] == "60"
    assert json.loads(middleware(RequestFactory().get("/api/v1/x/")).content)["code"] == "maintenance"
    assert called == []
    assert middleware(RequestFactory().get("/health/release/")).status_code in {200, 503}
    assert called == []


def test_administrative_mode_closes_all_but_the_open_routes(store_file):
    enter(InstanceStore(store_file), message="Migração de dados", ends_at=None, pause_jobs=True, source="t")
    called = []
    middleware = _middleware(called)
    factory = RequestFactory()
    for path in ("/", "/profile/", "/api/v1/me/", "/accounts/signup/", "/accounts/password/reset/", "/nao-existe/"):
        response = middleware(factory.get(path))
        assert response.status_code == 503, path
        assert response["Retry-After"] == "300"
    assert "Migração de dados" in middleware(factory.get("/")).content.decode()
    assert called == []
    for path in (
        "/accounts/login/",
        "/accounts/logout/",
        "/accounts/2fa/authenticate/",
        "/platform/",
        "/platform/users/",
    ):
        request = factory.get(path)
        assert middleware(request).status_code == 200, path
        assert request.skeleton_maintenance.message == "Migração de dados"
    assert len(called) == 5


def test_open_routes_are_a_closed_list_by_name():
    factory = RequestFactory()
    assert maintenance_open_route(factory.get("/accounts/login/"))
    assert maintenance_open_route(factory.get("/accounts/reauthenticate/"))
    assert maintenance_open_route(factory.get("/accounts/oidc/corp/login/callback/"))
    assert maintenance_open_route(factory.get("/platform/instance/"))
    assert not maintenance_open_route(factory.get("/accounts/signup/"))
    assert not maintenance_open_route(factory.get("/accounts/email/"))
    assert not maintenance_open_route(factory.get("/accounts/password/change/"))
    assert not maintenance_open_route(factory.get("/"))


def test_scheduled_end_and_independence_from_the_deployment_marker(tmp_path, store_file):
    store = InstanceStore(store_file)
    marker = tmp_path / "maintenance"
    enter(store, message="", ends_at=datetime.now(UTC) + timedelta(hours=1), pause_jobs=True, source="t")
    called = []
    middleware = _middleware(called, marker)
    marker.touch()
    assert middleware(RequestFactory().get("/")).status_code == 503
    marker.unlink()  # The agent finishing a deployment leaves the administrative mode on.
    assert middleware(RequestFactory().get("/")).status_code == 503
    with closing(sqlite3.connect(store_file)) as connection:  # The scheduled end passed.
        connection.execute(
            "UPDATE maintenance SET ends_at = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),)
        )
        connection.commit()
    maintenance.reader_for(store_file)._cached = None
    assert middleware(RequestFactory().get("/")).status_code == 200
    assert read_state(store).active, "Expiry is computed on read: nothing was written."


def test_deployment_smoke_requests_pass_both_modes(tmp_path, store_file):
    token = "s" * 40
    token_file = tmp_path / "smoke"
    token_file.write_text(token)
    marker = tmp_path / "maintenance"
    marker.touch()
    enter(InstanceStore(store_file), message="", ends_at=None, pause_jobs=True, source="t")
    called = []
    with override_settings(
        SKELETON_MAINTENANCE_MARKER=str(marker),
        SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE=str(token_file),
        SKELETON_DEPLOYMENT_SMOKE_PATHS=("/",),
    ):
        middleware = MaintenanceMiddleware(lambda request: called.append(request) or HttpResponse("ok"))
        assert middleware(RequestFactory().get("/", HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token)).status_code == 200
        marker.unlink()
        assert middleware(RequestFactory().get("/", HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token)).status_code == 200
        assert middleware(RequestFactory().get("/", HTTP_X_SKELETON_DEPLOYMENT_SMOKE="x" * 40)).status_code == 503


def test_full_stack_answers_before_session_tokens_or_database(store_file):
    enter(InstanceStore(store_file), message="", ends_at=None, pause_jobs=True, source="t")
    client = Client()
    response = client.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer skt_" + "a" * 40)
    assert response.status_code == 503 and response.json()["code"] == "maintenance"
    assert client.get("/").status_code == 503
    assert client.get("/health/live/").status_code == 200


def test_metrics_label_maintenance_answers_without_the_path(store_file):
    request = RequestFactory().get("/o/secret-org/")
    maintenance_response(request, message=None, ends_at=None, retry_after=300)
    assert route_label(request) == MAINTENANCE_ROUTE


def test_cli_enters_and_leaves_without_the_database(store_file):
    def run(*args):
        output = StringIO()
        call_command("maintenance", *args, stdout=output)
        return output.getvalue()

    assert "Manutenção administrativa: inativa" in run("status")
    out = run("on", "--message", "Troca de servidor", "--for", "2h", "--keep-jobs")
    assert "Manutenção ativa" in out and "jobs ativos" in out
    state = read_state(InstanceStore(store_file))
    assert state.is_active() and state.message == "Troca de servidor" and not state.pause_jobs
    assert timedelta(hours=1, minutes=59) < state.ends_at - datetime.now(UTC) <= timedelta(hours=2)
    status = run("status")
    assert "ativa" in status and "Troca de servidor" in status and "cli:maintenance-on" in status
    assert "Manutenção encerrada." in run("off")
    assert "já estava inativa" in run("off")
    run("on", "--until", (datetime.now(UTC) + timedelta(days=1)).isoformat())
    assert read_state(InstanceStore(store_file)).pause_jobs, "Jobs pause by default."
    for bad in (("on", "--for", "2x"), ("on", "--until", "2030-01-01T00:00"), ("on", "--message", "a\x07")):
        with pytest.raises(CommandError):
            run(*bad)
    with pytest.raises(CommandError):
        run("on", "--until", (datetime.now(UTC) - timedelta(days=1)).isoformat())


def test_old_marker_contract_kept_for_release_probe():
    state = DeploymentInventory(0, {}, (), (), ())
    with override_settings(SKELETON_DEPLOYMENT_STATE=state, SKELETON_MAINTENANCE_MARKER=None):
        middleware = MaintenanceMiddleware(lambda request: HttpResponse("ok"))
        assert json.loads(middleware(RequestFactory().get("/health/release/")).content)["generation"] == 0


# --- /platform/ guard and form (#447) ---------------------------------------------------------


class _User:
    def __init__(self, *, staff=False, superuser=False, authenticated=True):
        self.is_authenticated = authenticated
        self.is_active = True
        self.deleted_at = None
        self.is_staff = staff
        self.is_superuser = superuser


def test_platform_guard_lets_only_full_administrators_through_during_the_mode(store_file):
    from skeleton.web.maintenance import MaintenancePlatformGuardMiddleware

    state = enter(InstanceStore(store_file), message="Janela", ends_at=None, pause_jobs=True, source="t")
    called = []
    guard = MaintenancePlatformGuardMiddleware(lambda request: called.append(request) or HttpResponse("ok"))
    factory = RequestFactory()
    people = {
        "anonymous": _User(authenticated=False),
        "staff only": _User(staff=True),
        "superuser only": _User(superuser=True),
        "member": _User(),
    }
    for name, user in people.items():
        request = factory.get("/platform/audit/")
        request.user, request.skeleton_maintenance = user, state
        response = guard(request)
        assert response.status_code == 503, name
        assert "next=%2Fplatform%2Fmaintenance%2F" in response.content.decode()
    request = factory.get("/platform/audit/")
    request.user, request.skeleton_maintenance = _User(staff=True, superuser=True), state
    assert guard(request).status_code == 200
    outside = factory.get("/accounts/login/")  # Open routes outside /platform/ are not this guard's business.
    outside.user, outside.skeleton_maintenance = _User(), state
    assert guard(outside).status_code == 200
    no_mode = factory.get("/platform/audit/")  # Without the mode the platform guards decide, as before.
    no_mode.user = _User()
    assert guard(no_mode).status_code == 200
    assert len(called) == 3


def test_enter_form_validates_confirmation_end_and_message():
    from skeleton.web.platform.maintenance_views import EnterMaintenanceForm

    valid = EnterMaintenanceForm({"message": "Janela", "pause_jobs": "on", "confirm": "on"})
    assert valid.is_valid(), valid.errors
    assert valid.cleaned_data["pause_jobs"] is True and valid.cleaned_data["ends_at"] is None
    assert EnterMaintenanceForm().initial == {} and EnterMaintenanceForm().fields["pause_jobs"].initial is True
    assert "confirm" in EnterMaintenanceForm({"message": "x"}).errors
    past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    assert "ends_at" in EnterMaintenanceForm({"ends_at": past, "confirm": "on"}).errors
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    assert EnterMaintenanceForm({"ends_at": future, "confirm": "on"}).is_valid()
    assert "message" in EnterMaintenanceForm({"message": "x" * 501, "confirm": "on"}).errors
    assert "message" in EnterMaintenanceForm({"message": "a\x1bb", "confirm": "on"}).errors
    assert "message" in EnterMaintenanceForm({"message": "a‮b", "confirm": "on"}).errors


def test_middleware_order_check_requires_the_guard_after_authentication(settings):
    from skeleton.system.checks.contract import record_contract_checks

    guard = "skeleton.web.maintenance.MaintenancePlatformGuardMiddleware"
    auth = "django.contrib.auth.middleware.AuthenticationMiddleware"
    assert guard in settings.MIDDLEWARE
    assert settings.MIDDLEWARE.index(auth) < settings.MIDDLEWARE.index(guard)

    def e009():
        return [error for error in record_contract_checks(None) if error.id == "skeleton.E009"]

    assert e009() == []
    middleware = [item for item in settings.MIDDLEWARE if item != guard]
    settings.MIDDLEWARE = [*middleware[: middleware.index(auth)], guard, *middleware[middleware.index(auth) :]]
    assert e009()


# --- Jobs (#448) ------------------------------------------------------------------------------


def test_jobs_pause_on_the_marker_or_on_the_mode_that_asks_for_it(tmp_path, store_file):
    from skeleton.jobs.worker import maintenance_paused

    store = InstanceStore(store_file)
    marker = tmp_path / "maintenance"
    future = datetime.now(UTC) + timedelta(hours=1)
    with override_settings(SKELETON_MAINTENANCE_MARKER=str(marker)):
        cases = []
        for deploying in (False, True):
            for mode in ("off", "pause", "keep", "expired"):
                marker.touch() if deploying else marker.unlink(missing_ok=True)
                if mode == "off":
                    leave(store, source="t")
                else:
                    enter(store, message="", ends_at=future, pause_jobs=mode != "keep", source="t")
                    if mode == "expired":
                        with closing(sqlite3.connect(store_file)) as connection:
                            past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
                            connection.execute("UPDATE maintenance SET ends_at = ?", (past,))
                            connection.commit()
                maintenance.reader_for(store_file)._cached = None
                cases.append(((deploying, mode), maintenance_paused()))
    expected = {(False, "off"): False, (False, "pause"): True, (False, "keep"): False, (False, "expired"): False}
    expected |= {(True, mode): True for mode in ("off", "pause", "keep", "expired")}
    assert dict(cases) == expected


def test_jobs_fail_closed_when_the_instance_store_cannot_be_read(store_file):
    from skeleton.jobs.worker import maintenance_paused

    store_file.parent.mkdir(parents=True)
    store_file.write_bytes(b"not a sqlite database at all" * 10)
    maintenance._readers.pop(str(store_file), None)
    assert maintenance_paused()
    maintenance._readers.pop(str(store_file), None)


def test_queue_health_does_not_report_lag_while_paused():
    from skeleton.jobs.operations import QueueHealth

    health = QueueHealth(due=3, oldest_due_lag_s=900.0, dead=2, live_workers=0, stale_workers=1)
    assert health.problems(max_lag_s=1, max_dead=None, require_worker=False) == ["queue_lag 900s > 1s"]
    assert health.problems(max_lag_s=1, max_dead=None, require_worker=False, paused=True) == []
    paused = health.problems(max_lag_s=1, max_dead=0, require_worker=True, paused=True)
    assert paused == ["dead_jobs 2 > 0", "no_live_worker"]  # Only the lag is expected during a pause.
