"""Request, job and SQL spans (#176): linked, tagged with skeleton.request_id, without PII."""

import pytest
from django.db import connection
from django.test import Client
from django.urls import reverse
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from skeleton.accounts.models import User
from skeleton.accounts.services import bootstrap_identity
from skeleton.jobs import registry
from skeleton.jobs.registry import JobType, register_job_type
from skeleton.jobs.services import enqueue
from skeleton.observability import otel, tracing
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_job_types

pytestmark = pytest.mark.usefixtures("skeleton_db")
EMAIL = "traced-person@example.test"
PASSWORD = "Traced-Password!176"
NAME = "Traced Person Name"
SLUG = "traced-org"


def lookup_by_email(**kwargs):
    # A parameterized query whose parameter is an email: it must never reach a span.
    assert User.all_objects.filter(email=EMAIL).exists()


@pytest.fixture(autouse=True)
def job_types(monkeypatch):
    with isolated_job_types(keep_existing=True):
        register_job_type(JobType("test.tracing", lookup_by_email, {}, max_attempts=1))
        yield


@pytest.fixture
def spans(settings):
    settings.MIDDLEWARE = list(settings.MIDDLEWARE)  # the instrumentation edits it; the fixture restores it
    memory = InMemorySpanExporter()
    provider = otel.build_provider(memory, processor=SimpleSpanProcessor)
    tracing.use_tracer(otel.instrument(provider))
    connection.ensure_connection()
    PsycopgInstrumentor.instrument_connection(connection.connection, tracer_provider=provider)
    try:
        yield memory
    finally:
        PsycopgInstrumentor.uninstrument_connection(connection.connection)
        otel.uninstrument()
        tracing.use_tracer(None)
        provider.shutdown()


@pytest.fixture
def identity():
    return bootstrap_identity(email=EMAIL, organization=SLUG, name=NAME, password=PASSWORD)


def assert_no_pii(finished, *values):
    for span in finished:
        text = span.to_json()
        assert "db.statement" not in text and "db.query.text" not in text and "http.url" not in text
        for value in values:
            assert value not in text, (span.name, value)


def descends_from(span, ancestor, by_id):
    while span.parent is not None:
        if span.parent.span_id == ancestor.context.span_id:
            return True
        span = by_id.get(span.parent.span_id)
        if span is None:
            return False
    return False


def test_request_and_sql_spans_share_the_request_id(spans, identity):
    user, organization = identity
    client = Client()
    client.post(reverse("account_login"), {"login": EMAIL, "password": PASSWORD})
    spans.clear()
    response = client.get(f"/o/{organization.pk}/?email={EMAIL}&name={NAME}")
    request_id = response["X-Request-ID"]

    finished = spans.get_finished_spans()
    by_id = {span.context.span_id: span for span in finished}
    servers = [span for span in finished if span.kind is SpanKind.SERVER]
    assert len(servers) == 1
    server = servers[0]
    assert server.name == "GET o/<uuid:organization_id>/"
    assert server.attributes["skeleton.request_id"] == request_id
    assert server.attributes["http.route"] == "o/<uuid:organization_id>/"
    sql = [span for span in finished if "db.system" in span.attributes or "db.system.name" in span.attributes]
    assert sql, "the request runs SQL"
    for span in sql:
        assert span.attributes["skeleton.request_id"] == request_id
        assert span.context.trace_id == server.context.trace_id
        assert descends_from(span, server, by_id)
        assert span.name.isalpha() or span.name == "SQL"
    assert_no_pii(finished, EMAIL, PASSWORD, NAME, SLUG, str(organization.pk), str(user.pk))


def test_login_with_email_and_password_leaves_no_trace_of_them(spans, identity):
    """Security negative: the login query filters by email; neither it nor the password is exported."""
    response = Client().post(reverse("account_login"), {"login": EMAIL, "password": PASSWORD})
    assert response.status_code == 302
    finished = spans.get_finished_spans()
    assert any("db.system" in span.attributes or "db.system.name" in span.attributes for span in finished)
    assert_no_pii(finished, EMAIL, PASSWORD, NAME)


def test_job_span_parents_its_sql_and_carries_a_request_id(spans, identity):
    user, organization = identity
    with system_context(reason="test:tracing", organization_id=organization.pk):
        receipt = enqueue("test.tracing", organization_id=organization.pk)
    spans.clear()
    outcomes = run_pending_jobs()
    assert [outcome.state for outcome in outcomes if outcome.job_type == "test.tracing"] == ["succeeded"]

    finished = spans.get_finished_spans()
    by_id = {span.context.span_id: span for span in finished}
    jobs = [span for span in finished if span.name == "job test.tracing"]
    assert len(jobs) == 1
    job = jobs[0]
    request_id = job.attributes["skeleton.request_id"]
    assert job.kind is SpanKind.CONSUMER
    assert job.attributes["skeleton.job_id"] == str(receipt.id)
    assert job.attributes["skeleton.job_state"] == "succeeded"
    children = [span for span in finished if descends_from(span, job, by_id)]
    assert children and all(span.attributes["skeleton.request_id"] == request_id for span in children)
    assert_no_pii(finished, EMAIL, NAME, SLUG, str(organization.pk), str(user.pk))


def test_failed_job_span_records_the_error_class_only(spans, identity, monkeypatch):
    user, organization = identity

    def explode(**kwargs):
        raise ValueError(f"cannot handle {EMAIL}")

    monkeypatch.setitem(registry._registry, "test.tracing", JobType("test.tracing", explode, {}, max_attempts=1))
    with system_context(reason="test:tracing", organization_id=organization.pk):
        enqueue("test.tracing", organization_id=organization.pk)
    spans.clear()
    run_pending_jobs()
    (job,) = [span for span in spans.get_finished_spans() if span.name == "job test.tracing"]
    assert job.attributes["skeleton.job_state"] == "dead"
    assert job.attributes["error.type"] == "ValueError"
    assert_no_pii(spans.get_finished_spans(), EMAIL)
