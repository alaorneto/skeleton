"""OpenTelemetry traces (#176): requests, jobs and SQL, exported over OTLP, without PII.

Tracing turns on only when the ``[otel]`` extra is installed and an OTLP endpoint is configured
by the standard variables (``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` or
``OTEL_EXPORTER_OTLP_ENDPOINT``; ``OTEL_SDK_DISABLED=true`` turns it off). Everything else
(service name, headers, sampler, resource attributes) is read by the SDK from ``OTEL_*``.

This module never imports ``opentelemetry`` by itself; :mod:`skeleton.observability.otel` does,
and only once tracing is configured. The worker calls :func:`job_span` unconditionally: without
tracing it is a no-op.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from . import OTEL_DISTRIBUTIONS, installed

ENDPOINT_VARIABLES = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")

_tracer: Any = None


def tracing_requested(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    if environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    return any(environ.get(name, "").strip() for name in ENDPOINT_VARIABLES)


def tracing_enabled() -> bool:
    return tracing_requested() and installed(OTEL_DISTRIBUTIONS)


def configure_tracing() -> bool:
    """Called from ``AppConfig.ready()``, before the WSGI handler loads the middleware."""
    global _tracer
    if _tracer is not None or not tracing_enabled():
        return False
    from opentelemetry import trace

    from .otel import build_provider, instrument

    provider = build_provider()
    trace.set_tracer_provider(provider)
    _tracer = instrument(provider)
    return True


def use_tracer(tracer: Any) -> None:
    """Tests: route job spans to ``tracer`` (``None`` turns them off again)."""
    global _tracer
    _tracer = tracer


@contextmanager
def job_span(job: Any) -> Iterator[Any]:
    """A ``job <type>`` span around one execution; its SQL spans are children of it."""
    if _tracer is None:
        yield None
        return
    from opentelemetry.trace import SpanKind

    attributes = {
        "skeleton.job_id": str(job.pk),
        "skeleton.job_type": str(job.job_type),
        "skeleton.job_queue": str(job.queue),
        "skeleton.job_attempt": int(job.attempts),
    }
    with _tracer.start_as_current_span(f"job {job.job_type}", kind=SpanKind.CONSUMER, attributes=attributes) as span:
        yield span


def record_job_outcome(span: Any, state: str, error_class: str = "") -> None:
    if span is None or not span.is_recording():
        return
    span.set_attribute("skeleton.job_state", state)
    if error_class:
        from opentelemetry.trace import Status, StatusCode

        span.set_attribute("error.type", error_class)
        span.set_status(Status(StatusCode.ERROR))
