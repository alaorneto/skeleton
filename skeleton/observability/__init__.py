"""Metrics, traces and error reports (#87): optional extras, each off until configured.

Nothing here imports ``prometheus_client``, ``opentelemetry`` or ``sentry_sdk`` unless its
setting is present: ``SKELETON_METRICS_TOKEN`` (extra ``[metrics]``), an ``OTEL_EXPORTER_OTLP_*``
endpoint (extra ``[otel]``) or ``SKELETON_SENTRY_DSN`` (extra ``[sentry]``). Whether an extra is
installed is read from the package metadata, which imports nothing.
"""

from __future__ import annotations

from importlib import metadata

METRICS_DISTRIBUTIONS = ("prometheus-client",)
OTEL_DISTRIBUTIONS = (
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-instrumentation-django",
    "opentelemetry-instrumentation-psycopg",
)
SENTRY_DISTRIBUTIONS = ("sentry-sdk",)


def installed(distributions: tuple[str, ...]) -> bool:
    """Whether every distribution of an extra is installed, without importing any of them."""
    for name in distributions:
        try:
            metadata.distribution(name)
        except metadata.PackageNotFoundError:
            return False
    return True
