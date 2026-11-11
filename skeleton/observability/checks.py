"""skeleton.W030 / skeleton.E030: an observability setting without its extra, or a weak metrics token."""

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, Warning, register

from . import METRICS_DISTRIBUTIONS, OTEL_DISTRIBUTIONS, SENTRY_DISTRIBUTIONS, installed

METRICS_TOKEN_MIN_LENGTH = 32


def _missing_extra(setting: str, extra: str, effect: str) -> Warning:
    return Warning(
        f"{setting} is set, but the optional extra [{extra}] is not installed: {effect}.",
        hint=f"Install django-skeleton[{extra}] or unset {setting}.",
        id="skeleton.W030",
    )


def metrics_problems() -> list[CheckMessage]:
    from .metrics import MIDDLEWARE

    token = getattr(settings, "SKELETON_METRICS_TOKEN", "") or ""
    if not token:
        return []
    problems: list[CheckMessage] = []
    if not installed(METRICS_DISTRIBUTIONS):
        problems.append(_missing_extra("SKELETON_METRICS_TOKEN", "metrics", "/metrics stays 404"))
    elif MIDDLEWARE not in getattr(settings, "MIDDLEWARE", ()):
        problems.append(
            Warning(
                "SKELETON_METRICS_TOKEN is set, but MetricsMiddleware is not in MIDDLEWARE: /metrics stays 404.",
                hint=f"Add {MIDDLEWARE!r} right after RequestCompletionMiddleware.",
                id="skeleton.W030",
            )
        )
    if len(token) < METRICS_TOKEN_MIN_LENGTH or any(character.isspace() for character in token):
        problems.append(
            Error(
                f"SKELETON_METRICS_TOKEN needs at least {METRICS_TOKEN_MIN_LENGTH} characters and no whitespace.",
                hint='Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"',
                id="skeleton.E030",
            )
        )
    return problems


def tracing_problems() -> list[CheckMessage]:
    from .tracing import tracing_requested

    if tracing_requested() and not installed(OTEL_DISTRIBUTIONS):
        return [_missing_extra("OTEL_EXPORTER_OTLP_ENDPOINT", "otel", "no span is exported")]
    return []


def sentry_problems() -> list[CheckMessage]:
    if getattr(settings, "SKELETON_SENTRY_DSN", "") and not installed(SENTRY_DISTRIBUTIONS):
        return [_missing_extra("SKELETON_SENTRY_DSN", "sentry", "no error is reported")]
    return []


@register(Tags.security)
def observability_checks(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    return [*metrics_problems(), *tracing_problems(), *sentry_problems()]
