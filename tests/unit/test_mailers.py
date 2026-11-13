"""EMAIL_URL becomes MAILERS["default"] (#127): each scheme, query options, refusals and redaction."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from skeleton.conf.mailers import CONSOLE, DUMMY, FILE, MEMORY, SMTP, mailer_from_url


def test_smtp_with_tls_carries_every_connection_option():
    assert mailer_from_url("smtp+tls://user:s%40nha@mail.example:587") == {
        "BACKEND": SMTP,
        "OPTIONS": {
            "host": "mail.example",
            "port": 587,
            "username": "user",
            "password": "s@nha",
            "use_tls": True,
            "use_ssl": False,
            "timeout": 30,
        },
    }


def test_smtp_variants_and_query_options():
    ssl = mailer_from_url("smtp+ssl://mail.example?timeout=5&ssl_certfile=/c.pem&ssl_keyfile=/k.pem")
    assert ssl["OPTIONS"] == {
        "host": "mail.example",
        "use_tls": False,
        "use_ssl": True,
        "timeout": 5,
        "ssl_certfile": "/c.pem",
        "ssl_keyfile": "/k.pem",
    }
    assert mailer_from_url("smtps://mail.example")["OPTIONS"]["use_tls"] is True
    plain = mailer_from_url("smtp://mail.example:25", timeout=None)["OPTIONS"]
    assert plain == {"host": "mail.example", "port": 25, "use_tls": False, "use_ssl": False}


def test_local_backends():
    assert mailer_from_url("consolemail://") == {"BACKEND": CONSOLE, "OPTIONS": {}}
    assert mailer_from_url("memorymail://") == {"BACKEND": MEMORY, "OPTIONS": {}}
    assert mailer_from_url("dummymail://") == {"BACKEND": DUMMY, "OPTIONS": {}}
    assert mailer_from_url("filemail:///var/mail/out") == {"BACKEND": FILE, "OPTIONS": {"file_path": "/var/mail/out"}}


@pytest.mark.parametrize(
    "url",
    [
        "gopher://mail.example",
        "smtp://mail.example?unknown=1",
        "smtp://mail.example?timeout=abc",
        "smtp://mail.example?timeout=0",
        "consolemail://?timeout=3",
        "filemail://",
        "smtp://",
    ],
)
def test_unknown_or_invalid_urls_are_refused(url):
    with pytest.raises(ImproperlyConfigured):
        mailer_from_url(url)


def test_the_password_is_redacted_in_error_reports(settings):
    from django.views.debug import SafeExceptionReporterFilter

    settings.MAILERS = {"default": mailer_from_url("smtp+tls://user:topsecret@mail.example:587")}
    exposed = SafeExceptionReporterFilter().get_safe_settings()
    assert "topsecret" not in repr(exposed)
    assert exposed["MAILERS"]["default"]["OPTIONS"]["host"] == "mail.example"


PRODUCTION_ENV = {
    "DJANGO_SETTINGS_MODULE": "config.settings.production",
    "DJANGO_SECRET_KEY": "disposable-settings-secret-for-the-mailers-test-0123456789",
    "DJANGO_ALLOWED_HOSTS": "production.example.test",
    "DATABASE_URL": "postgres://skeleton_runtime:fixture@127.0.0.1:5433/disposable",
    "CACHE_URL": "rediscache://:fixture@127.0.0.1:6380/0",
    "DEFAULT_FROM_EMAIL": "fixture@example.test",
    "MFA_ENCRYPTION_KEYS": "dV6nwLEp9c3A8-yiLskMRFbJaUMoXXJ-_SNkrulBj8I=",
}


def _production(email_url, tmp_path, *code):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("EMAIL_", "SKELETON_INSTANCE"))}
    env.update(PRODUCTION_ENV, EMAIL_URL=email_url, SKELETON_PROJECT_ROOT=str(tmp_path))
    script = "import json, django; django.setup(); from django.conf import settings; " + "; ".join(code)
    return subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_production_builds_the_smtp_mailer_and_refuses_anything_else(tmp_path):
    smtp = _production(
        "smtp+tls://user:topsecret@mail.example:587",
        tmp_path,
        "print(json.dumps(settings.MAILERS['default']))",
        "assert not any(settings.is_overridden(name) for name in ('EMAIL_BACKEND', 'EMAIL_HOST', 'EMAIL_TIMEOUT'))",
    )
    assert smtp.returncode == 0, smtp.stderr[-2000:]
    mailer = __import__("json").loads(smtp.stdout.strip().splitlines()[-1])
    assert mailer["BACKEND"] == SMTP and mailer["OPTIONS"]["host"] == "mail.example"
    console = _production("consolemail://", tmp_path, "print(settings.MAILERS)")
    assert console.returncode != 0 and "Produção exige SMTP configurado." in console.stderr
