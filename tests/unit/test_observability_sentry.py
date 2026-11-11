"""Sentry (#177): before_send removes bodies, cookies, tokens, emails and messages; off without a DSN."""

import json

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

from skeleton.common.http_logging import bound_request_id
from skeleton.instance.schema import CATALOG
from skeleton.observability import checks, sentry

EMAIL = "reported-person@example.test"
TOKEN = "Bearer-Secret-Token-177"
COOKIE = "sessionid=Cookie-Secret-177"
PASSWORD = "Body-Password-177"
REQUEST_ID = "6f1c2a9e-1d2b-4c3d-8e4f-5a6b7c8d9e0f"
SENSITIVE = (EMAIL, TOKEN, COOKIE, "Cookie-Secret-177", PASSWORD, "203.0.113.7", "/home/alice", "Alice Person")


def synthetic_event():
    """What a careless SDK configuration could send: every field carries something sensitive."""
    return {
        "event_id": "0" * 32,
        "level": "error",
        "platform": "python",
        "logger": "django.request",
        "server_name": "web-1",
        "environment": "production",
        "release": "skeleton@0.5.0",
        "transaction": "/o/<uuid:organization_id>/",
        "sdk": {"name": "sentry.python.django", "version": "2.74.0", "packages": [{"name": "x"}]},
        "message": f"Internal error for {EMAIL}",
        "logentry": {"message": "login failed for %s", "params": [EMAIL]},
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "module": "builtins",
                    "value": f"invalid email {EMAIL} with token {TOKEN}",
                    "mechanism": {"type": "django", "handled": False, "data": {"token": TOKEN}},
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "/home/alice/app/skeleton/web/views.py",
                                "abs_path": "/home/alice/app/skeleton/web/views.py",
                                "lineno": 42,
                                "function": "dashboard",
                                "module": "skeleton.web.views",
                                "in_app": True,
                                "context_line": f"    check({PASSWORD!r})",
                                "pre_context": [f"email = {EMAIL!r}"],
                                "post_context": [TOKEN],
                                "vars": {"password": PASSWORD, "request": f"<Request {EMAIL}>"},
                            }
                        ]
                    },
                }
            ]
        },
        "request": {
            "method": "POST",
            "url": f"https://app.example/o/1/?email={EMAIL}&token={TOKEN}",
            "query_string": f"email={EMAIL}&token={TOKEN}",
            "cookies": {"sessionid": "Cookie-Secret-177"},
            "headers": {"Authorization": f"Bearer {TOKEN}", "Cookie": COOKIE, "User-Agent": "Alice Person"},
            "data": {"password": PASSWORD, "email": EMAIL},
            "env": {"REMOTE_ADDR": "203.0.113.7"},
        },
        "user": {"id": "1", "email": EMAIL, "ip_address": "203.0.113.7", "username": "Alice Person"},
        "extra": {
            "event": "auth.login_failed",
            "status": 500,
            "reason_code": "invalid_credentials",
            "target_sysid": "not-a-uuid " + EMAIL,
            "request_id": REQUEST_ID,
            "email": EMAIL,
            "security_context": {"actor": EMAIL},
            "sys.argv": ["manage.py", PASSWORD],
        },
        "tags": {"email": EMAIL, "token": TOKEN},
        "contexts": {
            "trace": {"trace_id": "a" * 32, "span_id": "b" * 16, "data": {"email": EMAIL}},
            "runtime": {"name": "CPython", "version": "3.14.0", "build": "/home/alice"},
            "response": {"headers": {"Set-Cookie": COOKIE}},
        },
        "breadcrumbs": {
            "values": [
                {
                    "type": "default",
                    "category": "query",
                    "level": "info",
                    "timestamp": "2026-11-11T00:00:00Z",
                    "message": f"SELECT * FROM users WHERE email = '{EMAIL}'",
                    "data": {"params": [EMAIL, PASSWORD]},
                },
                {"category": f"http {TOKEN}", "message": TOKEN},
            ]
        },
        "threads": {"values": [{"name": EMAIL}]},
        "modules": {"django": "6.1.3"},
    }


def test_synthetic_event_leaves_without_sensitive_values():
    """Security negative: any surviving body, cookie, token, email, path or message fails the test."""
    clean = sentry.before_send(synthetic_event(), {})
    text = json.dumps(clean)
    for value in SENSITIVE:
        assert value not in text, value
    assert not {"user", "threads", "modules", "message"} & set(clean)
    assert clean["request"] == {"method": "POST"}
    assert clean["logentry"] == {"message": "auth.login_failed"}
    assert clean["tags"] == {"skeleton.event": "auth.login_failed", "skeleton.request_id": REQUEST_ID}
    assert clean["extra"] == {"status": 500, "reason_code": "invalid_credentials"}
    (exception,) = clean["exception"]["values"]
    assert exception == {
        "type": "ValueError",
        "module": "builtins",
        "mechanism": {"type": "django", "handled": False},
        "stacktrace": {
            "frames": [
                {
                    "filename": "views.py",
                    "lineno": 42,
                    "function": "dashboard",
                    "module": "skeleton.web.views",
                    "in_app": True,
                }
            ]
        },
    }
    assert clean["contexts"] == {
        "trace": {"trace_id": "a" * 32, "span_id": "b" * 16},
        "runtime": {"name": "CPython", "version": "3.14.0"},
    }
    assert clean["breadcrumbs"] == {
        "values": [{"type": "default", "category": "query", "level": "info", "timestamp": "2026-11-11T00:00:00Z"}, {}]
    }
    assert clean["transaction"] == "/o/<uuid:organization_id>/"
    assert clean["sdk"] == {"name": "sentry.python.django", "version": "2.74.0"}


def test_transactions_with_values_are_dropped():
    for value in (f"/u/{EMAIL}/", "/o/6f1c2a9e-1d2b-4c3d-8e4f-5a6b7c8d9e0f/", "/search?q=x"):
        assert "transaction" not in sentry.redact_event({"transaction": value})


def test_request_id_of_the_running_request_is_tagged():
    with bound_request_id(REQUEST_ID):
        assert sentry.before_send({"level": "error"})["tags"] == {"skeleton.request_id": REQUEST_ID}
    assert "tags" not in sentry.before_send({"extra": {"request_id": "not-a-uuid"}})


def test_stack_is_cut_to_the_logging_limit():
    frames = [{"filename": f"/srv/app/m{index}.py", "lineno": index, "function": "f"} for index in range(40)]
    clean = sentry.before_send({"exception": {"values": [{"type": "KeyError", "stacktrace": {"frames": frames}}]}})
    kept = clean["exception"]["values"][0]["stacktrace"]["frames"]
    assert len(kept) == 25 and kept[-1] == {"filename": "m39.py", "lineno": 39, "function": "f"}


class CaptureTransport(Transport):
    envelopes: list = []

    def capture_envelope(self, envelope):
        CaptureTransport.envelopes.append(envelope)


def test_real_client_sends_only_the_redacted_event():
    """The SDK itself, with our options and an in-memory transport: nothing reaches the network."""
    CaptureTransport.envelopes = []
    options = {
        key: value
        for key, value in sentry.sentry_options().items()
        if key not in {"dsn", "integrations", "auto_session_tracking"}
    }
    client = sentry_sdk.Client(
        dsn="https://public@o0.ingest.example.invalid/1",
        transport=CaptureTransport,
        default_integrations=False,
        **options,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        scope.set_user({"email": EMAIL, "ip_address": "203.0.113.7"})
        scope.set_tag("token", TOKEN)
        scope.set_extra("password", PASSWORD)
        scope.add_breadcrumb(category="query", message=f"SELECT '{EMAIL}'", data={"params": [PASSWORD]})
        with bound_request_id(REQUEST_ID):
            try:
                raise ValueError(f"cannot process {EMAIL} with {TOKEN}")
            except ValueError:
                sentry_sdk.capture_exception()
    client.flush(timeout=2)
    payloads = [
        item.payload.json for envelope in CaptureTransport.envelopes for item in envelope.items if item.type == "event"
    ]
    assert len(payloads) == 1
    text = json.dumps(payloads[0])
    for value in SENSITIVE:
        assert value not in text, value
    assert payloads[0]["exception"]["values"][0]["type"] == "ValueError"
    assert "value" not in payloads[0]["exception"]["values"][0]
    assert payloads[0]["tags"]["skeleton.request_id"] == REQUEST_ID
    assert "user" not in payloads[0]


def test_options_keep_pii_off():
    options = sentry.sentry_options()
    assert options["send_default_pii"] is False
    assert options["before_send"] is sentry.before_send
    assert options["max_request_body_size"] == "never"
    assert options["include_local_variables"] is False and options["include_source_context"] is False
    assert options["traces_sample_rate"] is None
    assert options["before_send_transaction"]({"type": "transaction"}, {}) is None


def test_nothing_is_initialized_without_a_dsn_or_the_extra(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sentry, "_configured", False)
    settings.SKELETON_SENTRY_DSN = ""
    assert sentry.configure_sentry() is False
    settings.SKELETON_SENTRY_DSN = "https://public@o0.ingest.example.invalid/1"
    monkeypatch.setattr(sentry, "installed", lambda distributions: False)
    assert sentry.configure_sentry() is False
    assert calls == []
    monkeypatch.setattr(sentry, "installed", lambda distributions: True)
    assert sentry.configure_sentry() is True
    assert sentry.configure_sentry() is False, "initialized once"
    (options,) = calls
    assert options["dsn"] == "https://public@o0.ingest.example.invalid/1"
    assert options["send_default_pii"] is False and options["before_send"] is sentry.before_send


def test_check_warns_when_the_dsn_is_set_without_the_extra(settings, monkeypatch):
    settings.SKELETON_SENTRY_DSN = "https://public@o0.ingest.example.invalid/1"
    monkeypatch.setattr(checks, "installed", lambda distributions: distributions != checks.SENTRY_DISTRIBUTIONS)
    assert [problem.id for problem in checks.sentry_problems()] == ["skeleton.W030"]
    settings.SKELETON_SENTRY_DSN = ""
    assert checks.sentry_problems() == []


def test_dsn_is_a_catalogued_secret():
    spec = CATALOG["sentry_dsn"]
    assert spec.env == "SKELETON_SENTRY_DSN" and spec.secret
    assert spec.clean("https://abc@o1.ingest.sentry.io/42") == "https://abc@o1.ingest.sentry.io/42"
    for invalid in ("not a dsn", "https://o1.ingest.sentry.io/42", "ftp://abc@host/1", "https://abc@host/"):
        with pytest.raises(ValueError):
            spec.clean(invalid)
