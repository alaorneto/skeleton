"""SearchService of the core service API (#583)."""

import dataclasses
from uuid import UUID, uuid4

import pytest

from skeleton.service_api.errors import ContextRequired, InvalidInput
from skeleton.service_api.search import SearchGroup, SearchHit, SearchResults, SearchService
from skeleton.testing.service_api import (
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


def test_contract():
    assert_service_contract(SearchService)


@pytest.fixture
def scenario():
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="search-service")
    scenario.locacao = scenario.create(scenario.a, owner=scenario.member, code="SS-1", title="Contrato de locação")
    scenario.create(scenario.a, owner=scenario.admin_a, code="SS-2", title="Contrato reservado", visibility="private")
    scenario.foreign = scenario.create(scenario.b, owner=scenario.admin_b, code="SB-1", title="Contrato estrangeiro")
    return scenario


def plain_values(value):
    """Only str, bool, int, UUID, tuples and frozen DTOs: no lazy translation, no model."""
    if dataclasses.is_dataclass(value):
        assert type(value).__dataclass_params__.frozen
        return all(plain_values(getattr(value, field.name)) for field in dataclasses.fields(value))
    if isinstance(value, tuple):
        return all(plain_values(item) for item in value)
    return type(value) in {str, bool, int, UUID}


@pytest.mark.needs_example_app
def test_search_reads_only_what_the_actor_reads(scenario, subtests):
    service = SearchService()
    with subtests.test("authorized path"):
        with scenario.as_member() as actor:
            results = service.search(actor=actor, query="  contrato   de  loc ")
        assert results == SearchResults(
            query="contrato de loc",
            groups=(
                SearchGroup(
                    table_key=TABLE,
                    label="Anotações",
                    hits=(SearchHit(sysid=scenario.locacao.pk, label="Contrato de locação"),),
                    has_more=False,
                ),
            ),
        )
        assert plain_values(results)
    with subtests.test("default deny"):
        with scenario.as_denied() as actor:
            assert service.search(actor=actor, query="contrato").groups == ()
    with subtests.test("A/B"):
        assert_tenant_isolated(
            scenario.caller(lambda actor: service.search(actor=actor, query="contrato")),
            foreign=[scenario.foreign, "Contrato estrangeiro"],
        )
    with subtests.test("disabled application"):
        with scenario.application_disabled():
            with scenario.as_member() as actor:
                assert service.search(actor=actor, query="contrato").groups == ()
    with subtests.test("other actor"):
        assert_rejects_other_actor(
            scenario.caller(lambda actor: service.search(actor=scenario.admin_a, query="contrato"))
        )
    for name, kwargs in (
        ("short query", {"query": "a"}),
        ("long query", {"query": "x" * 201}),
        ("limit 0", {"query": "contrato", "limit": 0}),
        ("limit 21", {"query": "contrato", "limit": 21}),
        ("string limit", {"query": "contrato", "limit": "5"}),
    ):
        with subtests.test(name), scenario.as_member() as actor, pytest.raises(InvalidInput):
            service.search(actor=actor, **kwargs)


@pytest.mark.needs_example_app
def test_search_requires_an_organization(scenario):
    from skeleton.tenancy.context import request_context

    with request_context(actor=scenario.member, request_id=uuid4(), source="test:search-service"):
        with pytest.raises(ContextRequired):
            SearchService().search(actor=scenario.member, query="contrato")
