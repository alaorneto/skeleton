"""Global search over the tables marked with ``TableSpec.global_search`` (#581)."""

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from skeleton.records.search import Group, global_search
from skeleton.system.registry import GlobalSearchPolicy
from skeleton.testing.registries import table_spec_override
from skeleton.testing.service_api import acting_as, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
KEY = "example_notes.note"


@pytest.fixture
def scenario():
    from examples.notes.models import Note
    from skeleton.tenancy.context import system_context

    scenario = build_service_scenario(Note, slug="global-search")
    scenario.notes = {
        "S-1": scenario.create(scenario.a, owner=scenario.member, code="S-1", title="Contrato de locação"),
        "S-2": scenario.create(scenario.a, owner=scenario.member, code="S-2", title="Ação judicial trabalhista"),
        "S-3": scenario.create(
            scenario.a, owner=scenario.admin_a, code="S-3", title="Contrato confidencial", visibility="private"
        ),
        "B-1": scenario.create(scenario.b, owner=scenario.admin_b, code="B-1", title="Contrato da outra organização"),
    }
    deleted = scenario.create(scenario.a, owner=scenario.member, code="S-4", title="Contrato excluído")
    with system_context(reason="fixture:global-search-deleted", organization_id=scenario.a.pk):
        deleted.delete()
    scenario.notes["S-4"] = deleted
    return scenario


def run(user, organization, term="contrato", limit=5):
    with acting_as(user, organization):
        return global_search(user=user, term=term, limit=limit)


def titles(groups):
    return {hit.label for group in groups for hit in group.hits}


def test_reads_only_what_the_actor_reads(scenario, subtests):
    notes = scenario.notes
    with subtests.test("authorized member"):
        groups = run(scenario.member, scenario.a)
        assert groups == (
            Group(
                table_key=KEY,
                label="Anotações",
                hits=groups[0].hits,
                has_more=False,
            ),
        )
        assert [(hit.sysid, hit.label) for hit in groups[0].hits] == [(notes["S-1"].pk, "Contrato de locação")]
    with subtests.test("prefix of the last word"):
        assert titles(run(scenario.member, scenario.a, "contr")) == {"Contrato de locação"}
    with subtests.test("private of another person: owner and administrator only"):
        assert titles(run(scenario.admin_a, scenario.a)) == {"Contrato de locação", "Contrato confidencial"}
    with subtests.test("A/B"):
        assert titles(run(scenario.admin_b, scenario.b)) == {"Contrato da outra organização"}
        assert titles(run(scenario.member, scenario.b)) == {"Contrato da outra organização"}
        assert "Contrato da outra organização" not in titles(run(scenario.admin_a, scenario.a, "contr"))
    with subtests.test("soft-deleted records never appear"):
        assert "Contrato excluído" not in titles(run(scenario.admin_a, scenario.a))
    with subtests.test("default deny"):
        assert run(scenario.denied, scenario.a) == ()
    with subtests.test("no match, no group"):
        assert run(scenario.member, scenario.a, "inexistente") == ()
    with subtests.test("unmarked table is left out even with search_fields"):
        with table_spec_override(KEY, global_search=None):
            assert run(scenario.member, scenario.a) == ()
    with subtests.test("limit and has_more"):
        one = run(scenario.admin_a, scenario.a, limit=1)
        assert len(one[0].hits) == 1 and one[0].has_more is True
        two = run(scenario.admin_a, scenario.a, limit=2)
        assert len(two[0].hits) == 2 and two[0].has_more is False


def test_private_record_of_the_member_appears_to_its_owner(scenario):
    scenario.create(scenario.a, owner=scenario.member, code="S-5", title="Contrato pessoal", visibility="private")
    assert "Contrato pessoal" in titles(run(scenario.member, scenario.a))
    assert "Contrato pessoal" in titles(run(scenario.admin_a, scenario.a))


def test_disabled_application_gives_nothing(scenario):
    with scenario.application_disabled():
        assert run(scenario.member, scenario.a) == ()
    assert run(scenario.member, scenario.a) != ()


def test_invalid_input_is_refused(scenario, subtests):
    for term in ("a", "x" * 201, 5, None):
        with subtests.test(term=term), pytest.raises(ValidationError):
            run(scenario.member, scenario.a, term)
    for limit in (0, 21, "5", True):
        with subtests.test(limit=limit), pytest.raises(ValidationError):
            run(scenario.member, scenario.a, limit=limit)


def test_query_count_does_not_depend_on_the_limit(scenario):
    for code in ("S-6", "S-7", "S-8"):
        scenario.create(scenario.a, owner=scenario.member, code=code, title=f"Contrato {code}")
    with acting_as(scenario.member, scenario.a):
        global_search(user=scenario.member, term="contrato", limit=1)  # Warm the per-process caches.
        with CaptureQueriesContext(connection) as few:
            global_search(user=scenario.member, term="contrato", limit=1)
        with CaptureQueriesContext(connection) as many:
            found = global_search(user=scenario.member, term="contrato", limit=20)
    assert len(found[0].hits) == 4
    assert len(few) == len(many)


def test_e043_refuses_incoherent_marks(subtests):
    from skeleton.system.checks import record_contract_checks

    def e043():
        return [error for error in record_contract_checks(None) if error.id == "skeleton.E043"]

    assert e043() == []
    for name, changes in (
        ("not exposed", {"expose_api": False}),
        ("no search_fields", {"search_fields": ()}),
        ("not a policy", {"global_search": True}),
        ("negative order", {"global_search": GlobalSearchPolicy(order=-1)}),
        ("order too large", {"global_search": GlobalSearchPolicy(order=10000)}),
        ("bool order", {"global_search": GlobalSearchPolicy(order=True)}),
        ("no title field", {"read_fields": ("body", "owner", "visibility", "owning_group")}),
    ):
        with subtests.test(name), table_spec_override(KEY, **changes):
            assert len(e043()) == 1
