"""Administrative maintenance mode (#446, #447) with the full stack in PostgreSQL as the runtime role.

The unit tests prove the gate before the database; here the real identities, tokens and the
platform guard run against the mode stored in a per-test instance SQLite.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.instance import maintenance
from skeleton.instance.maintenance import enter, leave
from skeleton.instance.store import InstanceStore
from skeleton.integrations.tokens.services import issue_personal_token
from skeleton.tenancy.context import request_context
from tools.security_fixtures import build_security_fixture

NOTE = "example_notes.note"


@pytest.fixture
def store(tmp_path, settings):
    path = tmp_path / "instance" / "instance.sqlite3"
    settings.SKELETON_MAINTENANCE_STORE = str(path)
    yield InstanceStore(path)
    maintenance._readers.pop(str(path), None)


def _refresh(store):
    """Forget the per-process cache: the tests do not wait the 5 s a running process may take."""
    maintenance.reader_for(store.path)._cached = None


@pytest.mark.needs_example_app
def test_api_token_gets_the_maintenance_answer(skeleton_db, store):
    access = build_security_fixture()
    with request_context(actor=access.alice, request_id=uuid4(), source="test:maintenance"):
        issued = issue_personal_token(
            actor=access.alice, name="CLI", scopes=[f"{NOTE}:read"], expires_at=timezone.now() + timedelta(days=7)
        )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {issued.text}")
    url = reverse("api:records", kwargs={"organization_id": access.a.pk, "table_key": NOTE})
    assert client.get(url).status_code == 200

    enter(store, message="Migração", ends_at=timezone.now() + timedelta(hours=1), pause_jobs=True, source="test")
    _refresh(store)
    response = client.get(url)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "maintenance" and body["detail"] == "Migração" and body["ends_at"]
    assert int(response["Retry-After"]) > 3000

    leave(store, source="test")
    _refresh(store)
    assert client.get(url).status_code == 200


# --- /platform/ during the mode (#447) -------------------------------------------------------

PASSWORD = "Maintenance-platform-Password-2026!"


def _csrf(client):
    return client.cookies["csrftoken"].value


def _person(email):
    from allauth.account.models import EmailAddress

    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:maintenance-person"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


def _login(user):
    from tests import support

    return support.login(user, password=PASSWORD)


def _totp(user):
    from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret

    from skeleton.tenancy.context import system_context

    secret = generate_totp_secret()
    with system_context(reason="test:maintenance-totp"):
        TOTP.activate(user, secret)
    return secret


def _code(secret):
    from allauth.mfa.totp.internal.auth import format_hotp_value, hotp_value, yield_hotp_counters_from_time

    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


def _mfa_login(user, secret):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"), {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": _csrf(client)}
    )
    assert response.status_code == 302 and response["Location"].startswith(reverse("mfa_authenticate"))
    assert client.get(response["Location"]).status_code == 200
    from django.core.cache import cache

    cache.clear()  # allauth refuses a TOTP code already used; the test logs in twice in the same window.
    response = client.post(reverse("mfa_authenticate"), {"code": _code(secret), "csrfmiddlewaretoken": _csrf(client)})
    assert response.status_code == 302, response.content.decode()[:500]
    return client


@pytest.fixture
def platform(skeleton_db):
    from skeleton.accounts.services import bootstrap_identity

    admin, organization = bootstrap_identity(
        email="maintenance-admin@example.test", organization="maintenance-org", name="Ops", password=PASSWORD
    )
    return admin, organization


def _enter(client, **fields):
    data = {"message": "Janela de manutenção", "ends_at": "", "pause_jobs": "on", "confirm": "on"}
    data.update(fields)
    data["csrfmiddlewaretoken"] = _csrf(client)
    return client.post(reverse("platform:maintenance-enter"), {k: v for k, v in data.items() if v is not None})


def test_admin_with_totp_enters_logs_in_during_the_mode_and_leaves(platform, store, settings, caplog):
    import logging
    import sqlite3
    from contextlib import closing

    from cryptography.fernet import Fernet

    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    settings.MFA_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    admin, _ = platform
    secret = _totp(admin)
    client = _mfa_login(admin, secret)
    assert client.get(reverse("platform:maintenance")).status_code == 200

    caplog.set_level(logging.INFO, logger="skeleton.security")
    end = (timezone.localtime() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    response = _enter(client, ends_at=end)
    assert response.status_code == 302, response.content.decode()[:2000]
    _refresh(store)
    state = maintenance.read_state(store)
    assert state.active and state.pause_jobs and state.message == "Janela de manutenção" and state.ends_at
    assert state.source == f"platform:{admin.pk}"
    anonymous = Client()
    assert anonymous.get("/").status_code == 503
    assert anonymous.get("/api/v1/me/").status_code == 503

    # A new session of the same administrator: login and MFA stay open, /platform/ shows the banner.
    other = _mfa_login(admin, secret)
    page = other.get(reverse("platform:overview"))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Manutenção ativa" in html and reverse("platform:maintenance") in html
    assert other.get("/profile/").status_code == 503  # Outside /platform/ even the administrator gets 503.

    response = other.post(reverse("platform:maintenance-exit"), {"csrfmiddlewaretoken": _csrf(other)})
    assert response.status_code == 302
    _refresh(store)
    assert not maintenance.read_state(store).active
    assert anonymous.get(reverse("account_login")).status_code == 200
    assert anonymous.get("/").status_code != 503

    with closing(sqlite3.connect(store.path)) as connection:
        rows = connection.execute(
            "SELECT action, source FROM change_log WHERE key = 'maintenance' ORDER BY id"
        ).fetchall()
    assert rows == [("enter", f"platform:{admin.pk}"), ("exit", f"platform:{admin.pk}")]
    events = [record for record in caplog.records if record.name == "skeleton.security"]
    names = [getattr(record, "event", "") for record in events]
    assert "platform.maintenance_entered" in names and "platform.maintenance_exited" in names
    assert not any("Janela" in repr(record.__dict__) for record in events)


def test_everyone_but_full_administrators_gets_503_on_platform(platform, store):
    from skeleton.accounts.platform import grant_platform_role
    from skeleton.tenancy.context import request_context

    admin, _ = platform
    auditor, operator, member = (_person(f"{name}@example.test") for name in ("auditor", "operator", "member"))
    with request_context(actor=admin, request_id=uuid4(), source="test:maintenance-roles"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
        grant_platform_role(actor=admin, user=operator, role="operator")
    clients = {user.email: _login(user) for user in (auditor, operator, member)}
    assert clients["auditor@example.test"].get(reverse("platform:audit")).status_code == 200
    enter(store, message="<script>alert(1)</script>", ends_at=None, pause_jobs=True, source="test")
    _refresh(store)
    for email, client in clients.items():
        for name in ("platform:overview", "platform:audit", "platform:updates", "platform:maintenance"):
            response = client.get(reverse(name))
            assert response.status_code == 503, (email, name)
            html = response.content.decode()
            assert "<script>alert" not in html and "&lt;script&gt;" in html
            assert "next=%2Fplatform%2Fmaintenance%2F" in html  # The login link comes back to the page.
        assert client.post(reverse("platform:maintenance-exit")).status_code == 503
    assert maintenance.read_state(store).active
    assert Client().get(reverse("platform:overview")).status_code == 503
    assert _login(admin).get(reverse("platform:overview")).status_code == 200


def test_changes_need_csrf_recent_authentication_and_the_confirmation(platform, store):
    admin, _ = platform
    client = _login(admin)
    client.get(reverse("platform:maintenance"))
    response = _enter(client, confirm=None)
    assert response.status_code == 400 and "Marque a confirmação" in response.content.decode()
    past = (timezone.localtime() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    assert _enter(client, ends_at=past).status_code == 400
    assert _enter(client, message="x" * 501).status_code == 400
    assert _enter(client, message="bad\x07bell").status_code == 400
    assert not maintenance.read_state(store).active

    assert client.post(reverse("platform:maintenance-enter"), {"confirm": "on"}).status_code == 403  # No CSRF.
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = _enter(client)
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    assert not maintenance.read_state(store).active

    enter(store, message="", ends_at=None, pause_jobs=False, source="test")
    _refresh(store)
    assert client.get(reverse("account_reauthenticate")).status_code == 200  # Open during the mode.
    response = client.post(reverse("platform:maintenance-exit"), {"csrfmiddlewaretoken": _csrf(client)})
    assert response["Location"].startswith(reverse("account_reauthenticate"))
    assert maintenance.read_state(store).active


def test_admin_without_totp_is_sent_to_the_mfa_activation_that_works_during_the_mode(platform, store, settings):
    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    admin, _ = platform
    client = _login(admin)
    enter(store, message="", ends_at=None, pause_jobs=True, source="test")
    _refresh(store)
    response = client.get(reverse("platform:maintenance"))
    assert response.status_code == 302 and response["Location"] == reverse("mfa_index")
    assert client.get(reverse("mfa_index")).status_code == 200
    activation = client.get(reverse("mfa_activate_totp"))
    assert activation.status_code == 200 or activation["Location"].startswith(reverse("account_reauthenticate"))
