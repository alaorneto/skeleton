"""Tracing configuration and the redacting exporter (#176), with an in-memory exporter."""

import json

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from skeleton.common.http_logging import bound_request_id
from skeleton.observability import checks, otel, tracing

EMAIL = "traced-person@example.test"
SECRET = "Sql-Param-Secret-176"


@pytest.fixture
def exported():
    memory = InMemorySpanExporter()
    provider = otel.build_provider(memory, processor=SimpleSpanProcessor)
    yield provider, memory
    provider.shutdown()


@pytest.mark.parametrize(
    "environ,expected",
    [
        ({}, False),
        ({"OTEL_SERVICE_NAME": "app"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, True),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318/v1/traces"}, True),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318", "OTEL_SDK_DISABLED": "TRUE"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}, False),
    ],
)
def test_tracing_is_requested_only_by_an_otlp_endpoint(environ, expected):
    assert tracing.tracing_requested(environ) is expected


def test_unconfigured_tracing_does_nothing(monkeypatch):
    for name in tracing.ENDPOINT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert tracing.configure_tracing() is False
    with tracing.job_span(object()) as span:
        assert span is None
    tracing.record_job_outcome(None, "succeeded")


def test_exporter_keeps_only_allowlisted_attributes(exported):
    """Security negative: no SQL text or parameter, URL, client, user or exception message is exported."""
    provider, memory = exported
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("SELECT local_db", kind=SpanKind.CLIENT) as span:
        span.set_attributes(
            {
                "db.system": "postgresql",
                "db.name": "local_db",
                "db.statement": f"SELECT * FROM users WHERE email = '{EMAIL}'",
                "db.query.text": f"SELECT '{SECRET}'",
                "db.statement.parameters": str((EMAIL, SECRET)),
                "db.user": "skeleton_runtime",
                "net.peer.name": "db.internal",
            }
        )
    with tracer.start_as_current_span("GET o/<uuid:organization_id>/", kind=SpanKind.SERVER) as span:
        span.set_attributes(
            {
                "http.method": "GET",
                "http.route": "o/<uuid:organization_id>/",
                "http.status_code": 500,
                "http.url": f"https://app.example/o/x/?email={EMAIL}",
                "http.target": f"/o/x/?token={SECRET}",
                "url.query": f"email={EMAIL}",
                "http.user_agent": "Browser of a person",
                "net.peer.ip": "203.0.113.9",
                "client.address": "203.0.113.9",
                "enduser.id": EMAIL,
                "http.request.header.cookie": SECRET,
                "http.request.header.authorization": f"Bearer {SECRET}",
            }
        )
        span.record_exception(ValueError(f"bad email {EMAIL} with {SECRET}"))
        span.set_status(Status(StatusCode.ERROR, f"failed for {EMAIL}"))
    with bound_request_id("2b8c33a6-5a8e-4b8f-9d39-0d5e8f3a1c11"):
        with tracer.start_as_current_span("job test.tracing") as span:
            span.set_attribute("skeleton.job_type", "test.tracing")

    sql, request, job = memory.get_finished_spans()
    assert sql.name == "SELECT"
    assert dict(sql.attributes) == {"db.system": "postgresql", "db.name": "local_db"}
    assert dict(request.attributes) == {
        "http.method": "GET",
        "http.route": "o/<uuid:organization_id>/",
        "http.status_code": 500,
    }
    assert [(event.name, dict(event.attributes)) for event in request.events] == [
        ("exception", {"exception.type": "ValueError"})
    ]
    assert request.status.status_code is StatusCode.ERROR and not request.status.description
    assert job.attributes["skeleton.request_id"] == "2b8c33a6-5a8e-4b8f-9d39-0d5e8f3a1c11"
    assert job.attributes["skeleton.job_type"] == "test.tracing"
    exported_text = "".join(span.to_json() for span in (sql, request, job))
    for value in (EMAIL, SECRET, "203.0.113.9", "Browser of a person", "skeleton_runtime", "db.internal"):
        assert value not in exported_text, value
    json.loads(request.to_json())


def test_unusual_sql_names_collapse_to_sql():
    assert otel.safe_span_name("/* comment */ SELECT 1", {"db.system": "postgresql"}) == "SQL"
    assert otel.safe_span_name("select", {"db.system.name": "postgresql"}) == "SELECT"
    assert otel.safe_span_name("job webhooks.deliver", {}) == "job webhooks.deliver"


def test_request_id_reaches_spans_started_inside_the_block(exported):
    provider, memory = exported
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("outside"):
        pass
    with bound_request_id("8f0e1d3c-55aa-4c0e-8b0c-5a1d8a9e0c77") as request_id:
        with tracer.start_as_current_span("inside"):
            pass
    outside, inside = memory.get_finished_spans()
    assert "skeleton.request_id" not in outside.attributes
    assert inside.attributes["skeleton.request_id"] == request_id


def test_check_warns_when_an_endpoint_is_set_without_the_extra(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setattr(checks, "installed", lambda distributions: distributions != checks.OTEL_DISTRIBUTIONS)
    assert [problem.id for problem in checks.tracing_problems()] == ["skeleton.W030"]
    monkeypatch.setattr(checks, "installed", lambda distributions: True)
    assert checks.tracing_problems() == []
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    monkeypatch.setattr(checks, "installed", lambda distributions: False)
    assert checks.tracing_problems() == []
