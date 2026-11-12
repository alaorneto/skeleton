"""Prefix and similarity search in the reference picker (#181), only on readable records."""

import pytest
from django.core.exceptions import ValidationError

from skeleton.system import registry
from skeleton.system.search import trigram_available
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import acting_as, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")
KEY = "example_notes.note"


@pytest.fixture
def scenario():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="picker-search")
    scenario.create(scenario.a, owner=scenario.member, code="S-1", title="Contrato de locação")
    scenario.create(scenario.a, owner=scenario.member, code="S-2", title="Ação judicial trabalhista")
    scenario.create(scenario.b, owner=scenario.admin_b, code="B-1", title="Contrato da outra organização")
    return scenario


def picker(user, organization, term):
    from examples.notes.models import Note
    from skeleton.system.search import apply_label_search
    from skeleton.web.references import label_field
    from skeleton.web.services import record_queryset

    with acting_as(user, organization):
        rows = apply_label_search(record_queryset(registry.get_spec(KEY), user=user), label_field(Note), term)
        return [note.code for note in rows]


def test_picker_matches_prefixes_of_readable_records(scenario):
    assert picker(scenario.member, scenario.a, "contr") == ["S-1"]
    assert picker(scenario.member, scenario.a, "ação") == ["S-2"]
    assert picker(scenario.admin_b, scenario.b, "contr") == ["B-1"]
    with pytest.raises(ValidationError):
        picker(scenario.member, scenario.a, "c")


def test_picker_tolerates_typos_with_pg_trgm(scenario):
    with system_context(reason="test:search-trigram"):
        available = trigram_available()
    if not available:
        pytest.skip("pg_trgm is not installed in this PostgreSQL (the CI image has it).")
    assert picker(scenario.member, scenario.a, "cntrato")[0] == "S-1"
