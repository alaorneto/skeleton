"""Text search on declared fields (#180) and in the reference picker (#181)."""

from dataclasses import replace

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.system import registry
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
KEY = "example_notes.note"


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="search")
    scenario.create(scenario.a, owner=scenario.member, code="S-1", title="Contrato de locação")
    scenario.create(scenario.a, owner=scenario.member, code="S-2", title="Ação judicial trabalhista")
    scenario.create(scenario.a, owner=scenario.admin_a, code="S-3", title="Contrato confidencial", visibility="private")
    scenario.create(scenario.b, owner=scenario.admin_b, code="B-1", title="Contrato da outra organização")
    return scenario


def search(user, organization, term):
    from skeleton.web.services import filter_records, record_queryset

    spec = registry.get_spec(KEY)
    with acting_as(user, organization):
        rows = filter_records(record_queryset(spec, user=user), spec, {"q": term})
        return [note.code for note in rows]


def test_full_text_finds_variations_and_ignores_accents(scenario):
    assert search(scenario.member, scenario.a, "contratos") == ["S-1"]  # Stem: contrato.
    assert search(scenario.member, scenario.a, "acao") == ["S-2"]  # Without the accent.
    assert search(scenario.member, scenario.a, "LOCAÇÃO") == ["S-1"]


def test_search_never_crosses_the_authorization_or_the_organization(scenario):
    assert "S-3" not in search(scenario.member, scenario.a, "contrato")  # Private note of another owner.
    assert set(search(scenario.admin_a, scenario.a, "contrato")) == {"S-1", "S-3"}
    assert search(scenario.admin_b, scenario.b, "locação") == []
    client = Client()
    client.post(reverse("account_login"), {"login": scenario.member.email, "password": FIXTURE_PASSWORD})
    api = reverse("api:records", kwargs={"organization_id": scenario.a.pk, "table_key": KEY})
    data = client.get(api, {"q": "contrato"}).json()
    assert data["count"] == 1 and [row["code"] for row in data["results"]] == ["S-1"]
    page = reverse("web:records", kwargs={"organization_id": scenario.a.pk, "table_key": KEY})
    content = client.get(page, {"q": "contrato"}).content.decode()
    assert "S-1" in content and "S-2" not in content and "B-1" not in content


def test_short_terms_and_tables_without_search_are_refused(scenario, monkeypatch):
    with pytest.raises(ValidationError):
        search(scenario.member, scenario.a, "a")
    monkeypatch.setitem(registry._registry, KEY, replace(registry._registry[KEY], search_fields=()))
    with pytest.raises(ValidationError):
        search(scenario.member, scenario.a, "contrato")


def test_search_fields_must_be_readable_non_sensitive_text(monkeypatch):

    from skeleton.system.checks import record_contract_checks

    monkeypatch.setitem(registry._registry, KEY, replace(registry._registry[KEY], search_fields=("body",)))
    assert any(error.id == "skeleton.E031" for error in record_contract_checks(None))
