"""Service API base: caller contract, boundary values, pages and REST mapping (#97)."""

import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from skeleton.common.exceptions import ConcurrentUpdate, MissingTenantContext
from skeleton.service_api import (
    MAX_PAGE_SIZE,
    Conflict,
    ContextRequired,
    Forbidden,
    InvalidInput,
    NotFound,
    Page,
    ServiceAPI,
    Unavailable,
    as_utc,
    as_uuid,
    deprecated,
    paginate,
    service_method,
)
from skeleton.service_api.dto import decode_cursor, encode_cursor
from skeleton.tenancy import context as context_module

ORGANIZATION = uuid4()


@contextmanager
def fake_context(**values):
    """Set the context variable directly: these tests exercise the decorator, not the database."""
    token = context_module._context.set(context_module.Context(**values))
    try:
        yield
    finally:
        context_module._context.reset(token)


def person():
    return SimpleNamespace(pk=uuid4())


@dataclass(frozen=True, slots=True)
class Info:
    sysid: UUID
    name: str


class Sample(ServiceAPI):
    @service_method(read_only=True, since="1.0")
    def info(self, *, actor, name: str = "x") -> Info:
        return Info(sysid=actor.pk, name=name)

    @service_method(read_only=True, since="1.0", allow_system=True)
    def count(self, *, actor) -> int:
        return 1

    @service_method(read_only=True, since="1.0", tenant=False)
    def global_info(self, *, actor) -> str:
        return "ok"

    @service_method(read_only=True, since="1.0")
    def leak(self, *, actor, value: object) -> object:
        return value

    @service_method(read_only=False, since="1.0")
    def fail(self, *, actor, exc: Exception) -> None:
        raise exc

    @deprecated(since="1.1", removal="2.0")
    @service_method(read_only=True, since="1.0")
    def old(self, *, actor) -> int:
        return 2


def test_matching_actor_in_tenant_context_is_served():
    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION):
        assert Sample().info(actor=actor, name="n") == Info(sysid=actor.pk, name="n")


def test_actor_divergent_from_context_is_forbidden():
    actor, other = person(), person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION):
        with pytest.raises(Forbidden):
            Sample().info(actor=other)
        with pytest.raises(Forbidden):
            Sample().info(actor=SimpleNamespace(pk=None))
        with pytest.raises(Forbidden):
            Sample().info(actor=None)


def test_tenant_method_without_organization_requires_context():
    actor = person()
    with fake_context(actor=actor.pk):
        with pytest.raises(ContextRequired):
            Sample().info(actor=actor)
        assert Sample().global_info(actor=actor) == "ok"
    with pytest.raises(ContextRequired):
        Sample().info(actor=actor)


def test_system_caller_needs_allow_system_and_system_context():
    with fake_context(organization_id=ORGANIZATION, system=True):
        assert Sample().count(actor=None) == 1
        with pytest.raises(Forbidden):
            Sample().info(actor=None)
    with fake_context(organization_id=ORGANIZATION):
        with pytest.raises(Forbidden):
            Sample().count(actor=None)
    with fake_context(organization_id=ORGANIZATION, system=True):
        with pytest.raises(Forbidden):
            Sample().count(actor=person())


def test_positional_or_missing_actor_is_rejected():
    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION):
        with pytest.raises(TypeError):
            Sample.info(Sample(), actor)
        with pytest.raises(TypeError):
            Sample().info()


@pytest.mark.parametrize(
    "signature",
    [
        "def m(self, actor): pass",
        "def m(self, *, name): pass",
        "def m(cls, *, actor): pass",
        "def m(self, *args, actor): pass",
    ],
)
def test_signature_contract_is_checked_at_definition(signature):
    namespace = {}
    exec(signature, namespace)  # noqa: S102 - fixed test literal
    with pytest.raises(TypeError):
        service_method(read_only=True, since="1.0")(namespace["m"])


def test_decorator_requires_since_and_boolean_flags():
    with pytest.raises(TypeError):
        service_method(read_only=True, since="")
    with pytest.raises(TypeError):
        service_method(read_only="yes", since="1.0")


@pytest.mark.parametrize("value", [{"a": 1}, [1], {1}, (1, [2]), Info(sysid=uuid4(), name="x")])
def test_mutable_results_are_refused(value):
    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION):
        if isinstance(value, Info):
            assert Sample().leak(actor=actor, value=value) == value
            return
        with pytest.raises(TypeError):
            Sample().leak(actor=actor, value=value)


def test_models_and_querysets_are_refused():
    from skeleton.accounts.models import User

    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION):
        for value in (User(email="leak@example.test"), User.objects.none(), (User(),), Page((User(),), None, 1)):
            with pytest.raises(TypeError):
                Sample().leak(actor=actor, value=value)


@pytest.mark.parametrize(
    ("internal", "public", "code"),
    [
        (ConcurrentUpdate(), Conflict, "stale_revision"),
        (ValidationError({"title": ["Obrigatório."]}), InvalidInput, "invalid_input"),
        (ValidationError("x", code="unique"), Conflict, "integrity_conflict"),
        (PermissionDenied(), Forbidden, "forbidden"),
        (MissingTenantContext(), ContextRequired, "context_required"),
    ],
)
def test_internal_exceptions_never_cross_the_boundary(internal, public, code):
    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION):
        with pytest.raises(public) as raised:
            Sample().fail(actor=actor, exc=internal)
    assert raised.value.code == code
    if isinstance(raised.value, InvalidInput):
        assert dict(raised.value.errors) == {"title": ("Obrigatório.",)}


def test_unknown_exceptions_propagate_unchanged():
    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION):
        with pytest.raises(RuntimeError):
            Sample().fail(actor=actor, exc=RuntimeError("bug"))


def test_metadata_lists_service_methods_and_deprecations(monkeypatch):
    from skeleton.service_api import base

    logged = []
    monkeypatch.setattr(base, "logger", SimpleNamespace(warning=lambda event, **kwargs: logged.append(event)))
    methods = Sample.service_methods()
    assert set(methods) == {"info", "count", "global_info", "leak", "fail", "old"}
    assert methods["count"].allow_system is True
    assert methods["fail"].read_only is False
    assert Sample.deprecations()["old"].removal == "2.0"
    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert Sample().old(actor=actor) == 2
    assert any(issubclass(item.category, DeprecationWarning) for item in caught)
    assert logged == ["service_api.deprecated_call"]


def test_page_is_bounded_and_immutable():
    with pytest.raises(ValueError):
        Page((), None, MAX_PAGE_SIZE + 1)
    with pytest.raises(ValueError):
        Page((), None, 0)
    with pytest.raises(TypeError):
        Page([1], None, 10)
    with pytest.raises(ValueError):
        Page((1, 2), None, 1)
    page = Page((1,), None, 10)
    with pytest.raises(AttributeError):
        page.page_size = 50


def test_paginate_walks_with_opaque_cursor():
    rows = list(range(7))
    first = paginate(rows, convert=str, page_size=3)
    assert first.items == ("0", "1", "2")
    second = paginate(rows, convert=str, page_size=3, cursor=first.next_cursor)
    assert second.items == ("3", "4", "5")
    last = paginate(rows, convert=str, page_size=3, cursor=second.next_cursor)
    assert last.items == ("6",)
    assert last.next_cursor is None
    assert decode_cursor(encode_cursor(42)) == 42


@pytest.mark.parametrize("cursor", ["", "!!!", "bzotMQ", "eDox", "x" * 65, 3])
def test_invalid_cursor_is_invalid_input(cursor):
    with pytest.raises(InvalidInput):
        paginate([1, 2], convert=str, cursor=cursor)


@pytest.mark.parametrize("size", [0, MAX_PAGE_SIZE + 1, "10", True])
def test_page_size_above_limit_is_invalid_input(size):
    with pytest.raises(InvalidInput):
        paginate([1], convert=str, page_size=size)


def test_paginate_requires_ordered_queryset():
    from skeleton.accounts.models import User

    with pytest.raises(TypeError):
        paginate(User.objects.all().order_by(), convert=str)


def test_safe_conversions():
    value = uuid4()
    assert as_uuid(value) is value
    assert as_uuid(str(value)) == value
    for bad in ("not-a-uuid", 1, None, "x" * 100):
        with pytest.raises(InvalidInput):
            as_uuid(bad)
    local = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=-3)))
    assert as_utc(local) == datetime(2026, 1, 1, 15, tzinfo=UTC)
    assert as_utc(local).tzinfo is UTC
    with pytest.raises(ValueError):
        as_utc(datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (NotFound(), 404, "not_found"),
        (Unavailable(), 404, "not_found"),
        (Forbidden(), 403, "permission_denied"),
        (InvalidInput({"title": ["Obrigatório."]}), 400, "validation_error"),
        (Conflict("stale_revision"), 409, "stale_revision"),
    ],
)
def test_service_errors_map_to_http(error, status, code):
    from skeleton.api.errors import exception_handler

    response = exception_handler(error, {})
    assert response.status_code == status
    assert response.data["code"] == code
    if isinstance(error, InvalidInput):
        assert response.data["fields"] == {"title": ["Obrigatório."]}


def test_context_required_is_a_server_error():
    from skeleton.api.errors import exception_handler

    assert exception_handler(ContextRequired(), {}) is None
