"""``EMAIL_URL`` → ``MAILERS["default"]`` (#127).

Django 6.1 deprecates the per-connection email settings (``EMAIL_BACKEND``, ``EMAIL_HOST`` and
so on) in favour of ``MAILERS``; django-environ still produces only the old keys. The operation
interface stays ``EMAIL_URL``: django-environ parses it, and this module translates the result
into the ``OPTIONS`` each backend accepts. The 6.1 backends refuse unknown ``OPTIONS``, and an
unknown scheme or query parameter is refused here too, with ``ImproperlyConfigured``, rather
than ignored. Never define ``EMAIL_*`` connection settings beside ``MAILERS``: Django refuses
the mix.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

from django.core.exceptions import ImproperlyConfigured

SMTP = "django.core.mail.backends.smtp.EmailBackend"
CONSOLE = "django.core.mail.backends.console.EmailBackend"
FILE = "django.core.mail.backends.filebased.EmailBackend"
MEMORY = "django.core.mail.backends.locmem.EmailBackend"
DUMMY = "django.core.mail.backends.dummy.EmailBackend"
SCHEMES = {
    "smtp": SMTP,
    "smtps": SMTP,
    "smtp+tls": SMTP,
    "smtp+ssl": SMTP,
    "consolemail": CONSOLE,
    "filemail": FILE,
    "memorymail": MEMORY,
    "dummymail": DUMMY,
}
# Query parameters an EMAIL_URL may carry, by the OPTIONS name they become (SMTP only).
SMTP_QUERY = {"timeout": "timeout", "ssl_keyfile": "ssl_keyfile", "ssl_certfile": "ssl_certfile"}
DEFAULT_TIMEOUT = 30


def mailer_from_url(url: str, *, timeout: int | None = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """The ``MAILERS`` entry of an ``EMAIL_URL``; ``timeout`` is the SMTP default (#297)."""
    import environ  # type: ignore[import-untyped]

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        raise ImproperlyConfigured(f"EMAIL_URL: esquema desconhecido {scheme!r}.")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    backend = SCHEMES[scheme]
    unknown = sorted(set(query) - (set(SMTP_QUERY) if backend == SMTP else set()))
    if unknown:
        raise ImproperlyConfigured(f"EMAIL_URL: parâmetros desconhecidos {unknown}.")
    if backend == FILE:
        path = parts.path
        if not path:
            raise ImproperlyConfigured("EMAIL_URL: filemail:// exige um caminho.")
        return {"BACKEND": FILE, "OPTIONS": {"file_path": path}}
    if backend != SMTP:
        return {"BACKEND": backend, "OPTIONS": {}}
    config = environ.Env.email_url_config(url.split("?", 1)[0])
    options: dict[str, Any] = {
        "host": config.get("EMAIL_HOST") or None,
        "port": config.get("EMAIL_PORT"),
        "username": config.get("EMAIL_HOST_USER") or None,
        "password": config.get("EMAIL_HOST_PASSWORD") or None,
        "use_tls": bool(config.get("EMAIL_USE_TLS", False)),
        "use_ssl": bool(config.get("EMAIL_USE_SSL", False)),
    }
    if timeout is not None:
        options["timeout"] = timeout
    for name, option in SMTP_QUERY.items():
        if name in query:
            options[option] = _int(name, query[name]) if name == "timeout" else query[name]
    if not options["host"]:
        raise ImproperlyConfigured("EMAIL_URL: SMTP exige um host.")
    return {"BACKEND": SMTP, "OPTIONS": {key: value for key, value in options.items() if value is not None}}


def _int(name: str, value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ImproperlyConfigured(f"EMAIL_URL: {name} precisa ser um inteiro.") from None
    if number <= 0:
        raise ImproperlyConfigured(f"EMAIL_URL: {name} precisa ser positivo.")
    return number


def connection_for(url: str, *, timeout: int) -> Any:
    """A backend instance for one ``EMAIL_URL`` outside ``MAILERS`` (the setup's test message)."""
    from django.utils.module_loading import import_string

    entry = mailer_from_url(url, timeout=timeout)
    return import_string(entry["BACKEND"])(alias="setup", **entry["OPTIONS"])
