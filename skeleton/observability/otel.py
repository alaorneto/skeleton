"""The OpenTelemetry side of tracing (#176); imported only when tracing is configured.

Spans come from the official Django and psycopg instrumentations and from the worker
(:func:`skeleton.observability.tracing.job_span`). Two pieces keep them free of PII:

* :class:`RequestIdSpanProcessor` adds ``skeleton.request_id`` (the server-generated correlation
  id of :mod:`skeleton.common.http_logging`) to every span started inside a request or job;
* :class:`RedactingSpanExporter` is the last step before export and keeps only an allowlist of
  attributes. URLs, query strings, client addresses, user agents, SQL text and parameters,
  exception messages and stack traces never leave the process, whatever an instrumentation adds.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status, Tracer

from skeleton.common.http_logging import current_request_id

REQUEST_ID = "skeleton.request_id"
ALLOWED_ATTRIBUTES = frozenset(
    {
        REQUEST_ID,
        "skeleton.job_id",
        "skeleton.job_type",
        "skeleton.job_queue",
        "skeleton.job_attempt",
        "skeleton.job_state",
        "error.type",
        # HTTP: method, route pattern and status; never the URL, host, client or user agent.
        "http.method",
        "http.request.method",
        "http.route",
        "http.status_code",
        "http.response.status_code",
        "http.scheme",
        "url.scheme",
        "http.flavor",
        "network.protocol.version",
        # SQL: system, database and operation; never the statement or its parameters.
        "db.system",
        "db.system.name",
        "db.name",
        "db.namespace",
        "db.operation",
        "db.operation.name",
    }
)
DB_MARKERS = ("db.system", "db.system.name")
DEFAULT_EXCLUDED_URLS = "health/live/,health/ready/,metrics"
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,199}")


def safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (attributes or {}).items() if key in ALLOWED_ATTRIBUTES}


def safe_span_name(name: str, attributes: Mapping[str, Any] | None) -> str:
    """SQL spans keep only the leading keyword (``SELECT``); others are route or job names."""
    if any(marker in (attributes or {}) for marker in DB_MARKERS):
        keyword = (name or "").split(" ", 1)[0]
        return keyword.upper() if keyword.isalpha() and len(keyword) <= 32 else "SQL"
    return (name or "span")[:200]


def _safe_events(events: Sequence[Event]) -> list[Event]:
    """Exception events keep the exception type only: messages and stack traces carry data."""
    kept = []
    for event in events:
        kind = (event.attributes or {}).get("exception.type")
        if event.name == "exception" and isinstance(kind, str) and _TYPE_NAME.fullmatch(kind):
            kept.append(Event("exception", {"exception.type": kind}, timestamp=event.timestamp))
    return kept


def redact_span(span: ReadableSpan) -> ReadableSpan:
    return ReadableSpan(
        name=safe_span_name(span.name, span.attributes),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=safe_attributes(span.attributes),
        events=_safe_events(span.events),
        links=[Link(link.context) for link in span.links],
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanExporter(SpanExporter):
    """Wraps the real exporter; every span is rebuilt from the allowlist before it leaves."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([redact_span(span) for span in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


class RequestIdSpanProcessor(SpanProcessor):
    """Tags spans started inside a request or a job execution with its ``request_id``."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        request_id = current_request_id()
        if request_id:
            span.set_attribute(REQUEST_ID, request_id)


def response_hook(span: Any, request: Any, response: Any) -> None:
    """The request span starts before ``RequestCompletionMiddleware`` assigns the id; tag it at the end."""
    request_id = getattr(request, "request_id", None)
    if request_id and span is not None and span.is_recording():
        span.set_attribute(REQUEST_ID, str(request_id))


def build_provider(
    exporter: SpanExporter | None = None, *, processor: type[SpanProcessor] | None = None
) -> TracerProvider:
    """A provider whose only way out is the redacting exporter (OTLP/HTTP by default)."""
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()
    service = os.environ.get("OTEL_SERVICE_NAME") or "skeleton"
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(RequestIdSpanProcessor())
    provider.add_span_processor((processor or BatchSpanProcessor)(RedactingSpanExporter(exporter)))  # type: ignore[call-arg]
    return provider


def instrument(provider: TracerProvider) -> Tracer:
    """Django requests and psycopg statements, without SQL comments or parameters."""
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    DjangoInstrumentor().instrument(
        tracer_provider=provider,
        response_hook=response_hook,
        excluded_urls=os.environ.get("OTEL_PYTHON_DJANGO_EXCLUDED_URLS") or DEFAULT_EXCLUDED_URLS,
    )
    PsycopgInstrumentor().instrument(tracer_provider=provider, enable_commenter=False, capture_parameters=False)
    return provider.get_tracer("skeleton.jobs")


def uninstrument() -> None:
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    DjangoInstrumentor().uninstrument()
    PsycopgInstrumentor().uninstrument()
