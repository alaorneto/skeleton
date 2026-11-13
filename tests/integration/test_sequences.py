"""Gapless sequences per organization and period (#155)."""

from datetime import UTC, datetime

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from skeleton.common.exceptions import MissingTenantContext
from skeleton.sequences import registry
from skeleton.sequences.registry import SequenceSpec, register_sequence
from skeleton.sequences.services import next_number
from skeleton.tenancy.context import system_context
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

KEY = "example_notes.ticket"
JAN = datetime(2026, 1, 15, 12, tzinfo=UTC)
FEB = datetime(2026, 2, 1, 0, 30, tzinfo=UTC)
NEXT_YEAR = datetime(2027, 1, 1, tzinfo=UTC)


@pytest.fixture
def tenants(skeleton_db, monkeypatch):
    monkeypatch.setattr(registry, "_sequences", dict(registry._sequences))
    register_sequence(SequenceSpec(KEY, period="year", template="AT-{year}-{number:04d}", owner_app="example_notes"))
    register_sequence(SequenceSpec("example_notes.monthly", period="month", template="{period}/{number}"))
    register_sequence(SequenceSpec("example_notes.forever", period="none"))
    return build_security_fixture()


def as_module(module, function):
    """Run ``function`` from a frame whose module is ``module`` (how an app's code calls the service)."""
    namespace = {"__name__": module, "function": function}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    return namespace["call"]


def draw(fixture, organization, key=KEY, at=JAN):
    user = fixture.admin if organization == fixture.a else fixture.bob
    with actor_tenant(user, organization):
        return next_number(key, at=at)


def test_consecutive_numbers_with_the_declared_template(tenants):
    first, second = draw(tenants, tenants.a), draw(tenants, tenants.a)
    assert (first.number, first.formatted, first.period) == (1, "AT-2026-0001", "2026")
    assert (second.number, second.formatted) == (2, "AT-2026-0002")


def test_a_rollback_returns_the_number(tenants):
    draw(tenants, tenants.a)
    with actor_tenant(tenants.admin, tenants.a):
        with pytest.raises(RuntimeError), transaction.atomic():
            assert next_number(KEY, at=JAN).number == 2
            raise RuntimeError("business write failed")
        assert next_number(KEY, at=JAN).number == 2  # No gap.


def test_organizations_and_periods_are_independent(tenants):
    assert draw(tenants, tenants.a).number == 1
    assert draw(tenants, tenants.a).number == 2
    assert draw(tenants, tenants.b).formatted == "AT-2026-0001"
    assert draw(tenants, tenants.a, at=NEXT_YEAR).formatted == "AT-2027-0001"
    assert draw(tenants, tenants.a, "example_notes.monthly", JAN).formatted == "2026-01/1"
    assert draw(tenants, tenants.a, "example_notes.monthly", FEB).formatted == "2026-02/1"
    assert draw(tenants, tenants.a, "example_notes.forever", JAN).formatted == "1"
    assert draw(tenants, tenants.a, "example_notes.forever", NEXT_YEAR).formatted == "2"


def test_refusals_write_nothing(tenants):
    from skeleton.sequences.models import SequenceCounter

    with pytest.raises(MissingTenantContext), system_context(reason="test:sequence-global"):
        next_number(KEY, at=JAN)
    with actor_tenant(tenants.admin, tenants.a):
        with pytest.raises(ValidationError):
            next_number("example_notes.undeclared", at=JAN)
        with pytest.raises(ValidationError):
            next_number(KEY, at=datetime(2026, 1, 1))  # Naive datetime.
        assert not SequenceCounter.objects.exists()


def test_counters_are_audited_and_isolated(tenants):
    from skeleton.audit.models import AuditEvent
    from skeleton.sequences.models import SequenceCounter

    draw(tenants, tenants.a)
    draw(tenants, tenants.b)
    with actor_tenant(tenants.admin, tenants.a):
        [counter] = SequenceCounter.objects.all()
        assert counter.organization_id == tenants.a.pk and counter.last_value == 1
        operations = set(AuditEvent.objects.filter(record_sysid=counter.pk).values_list("operation", flat=True))
        assert operations == {"create", "update"}


def test_service_is_only_for_the_owning_app(tenants):
    from skeleton.service_api.errors import Forbidden, InvalidInput
    from skeleton.service_api.sequences import SequenceService

    service = SequenceService()
    with actor_tenant(tenants.admin, tenants.a):
        owned = as_module("examples.notes.services", service.next_number)(actor=tenants.admin, key=KEY, at=JAN)
        assert owned.formatted == "AT-2026-0001"
        with pytest.raises(Forbidden):
            as_module("examples.notes.services", service.next_number)(
                actor=tenants.admin, key="example_notes.monthly", at=JAN
            )
        with pytest.raises(Forbidden):
            service.next_number(actor=tenants.admin, key=KEY, at=JAN)  # Core code is not the owner app.
        with pytest.raises(InvalidInput):
            as_module("examples.notes.services", service.next_number)(actor=tenants.admin, key="nope.nope", at=JAN)


def test_service_contract_actor_and_context(tenants):
    from skeleton.service_api.errors import ContextRequired, Forbidden
    from skeleton.service_api.sequences import SequenceService
    from skeleton.testing.service_api import assert_service_contract

    assert_service_contract(SequenceService)
    call = as_module("examples.notes.services", SequenceService().next_number)
    with actor_tenant(tenants.admin, tenants.a):
        with pytest.raises(Forbidden):
            call(actor=tenants.alice, key=KEY, at=JAN)  # Not the actor of the context.
        with pytest.raises(Forbidden):
            call(actor=None, key=KEY, at=JAN)
    with pytest.raises(ContextRequired), system_context(reason="test:sequence-service-global"):
        call(actor=None, key=KEY, at=JAN)


def test_sequence_without_owner_is_forbidden_for_everyone(tenants):
    """A sequence without owner_app is usable by no caller of the service, app or not (#272)."""
    from skeleton.service_api.errors import Forbidden
    from skeleton.service_api.sequences import SequenceService

    service = SequenceService()
    with actor_tenant(tenants.admin, tenants.a):
        for call in (service.next_number, as_module("examples.notes.services", service.next_number)):
            with pytest.raises(Forbidden):
                call(actor=tenants.admin, key="example_notes.monthly", at=JAN)
    assert draw(tenants, tenants.a, key="example_notes.monthly").number == 1  # The internal function still works.
