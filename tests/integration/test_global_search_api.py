"""REST global search: ``GET /api/v1/o/<org>/search/`` (#582)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
KEY = "example_notes.note"
NOTE_READ = "example_notes.note:read"


@pytest.fixture
def scenario():
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="global-search-api")
    scenario.locacao = scenario.create(scenario.a, owner=scenario.member, code="S-1", title="Contrato de locação")
    scenario.create(scenario.a, owner=scenario.member, code="S-2", title="Ação judicial trabalhista")
    scenario.create(scenario.a, owner=scenario.admin_a, code="S-3", title="Contrato confidencial", visibility="private")
    scenario.create(scenario.b, owner=scenario.admin_b, code="B-1", title="Contrato da outra organização")
    return scenario


def login(user):
    client = Client()
    client.post(reverse("account_login"), {"login": user.email, "password": FIXTURE_PASSWORD})
    return client


def url(organization):
    return reverse("api:search", kwargs={"organization_id": organization.pk})


def token_client(user, scopes):
    from skeleton.integrations.tokens.services import issue_personal_token
    from skeleton.tenancy.context import request_context

    with request_context(actor=user, request_id=uuid4(), source="test:global-search-api"):
        issued = issue_personal_token(
            actor=user, name="CLI", scopes=list(scopes), expires_at=timezone.now() + timedelta(days=1)
        )
    return Client(HTTP_AUTHORIZATION=f"Bearer {issued.text}")


def labels(response):
    return {item["label"] for group in response.json()["results"] for item in group["items"]}


def test_session_search(scenario, subtests):
    member = login(scenario.member)
    with subtests.test("authorized member, prefix of the last word"):
        response = member.get(url(scenario.a), {"q": "contr"})
        assert response.status_code == 200 and response.headers["X-Request-ID"]
        assert response.json() == {
            "results": [
                {
                    "table_key": KEY,
                    "label": "Anotações",
                    "has_more": False,
                    "items": [{"sysid": str(scenario.locacao.pk), "label": "Contrato de locação"}],
                }
            ]
        }
    with subtests.test("limit and has_more"):
        admin = login(scenario.admin_a)
        group = admin.get(url(scenario.a), {"q": "contrato", "limit": "1"}).json()["results"][0]
        assert len(group["items"]) == 1 and group["has_more"] is True
        assert labels(admin.get(url(scenario.a), {"q": "contrato"})) == {"Contrato de locação", "Contrato confidencial"}
    with subtests.test("no match"):
        response = member.get(url(scenario.a), {"q": "inexistente"})
        assert response.status_code == 200 and response.json() == {"results": []}
    with subtests.test("A/B and private of another person"):
        assert "Contrato da outra organização" not in labels(member.get(url(scenario.a), {"q": "contrato"}))
        assert "Contrato confidencial" not in labels(member.get(url(scenario.a), {"q": "contrato"}))
        assert labels(member.get(url(scenario.b), {"q": "contrato"})) == {"Contrato da outra organização"}
    with subtests.test("default deny"):
        response = login(scenario.denied).get(url(scenario.a), {"q": "contrato"})
        assert response.status_code == 200 and response.json() == {"results": []}
    for params in (
        {},
        {"q": "a"},
        {"q": "x" * 201},
        {"q": "contr", "limit": "0"},
        {"q": "contr", "limit": "21"},
        {"q": "contr", "limit": "abc"},
        {"q": "contr", "page": "1"},
        {"q": ["contr", "loc"]},
    ):
        with subtests.test("invalid", params=params):
            response = member.get(url(scenario.a), params)
            assert response.status_code == 400 and response.json()["code"] == "invalid_filter"
    with subtests.test("POST is not allowed"):
        assert member.post(url(scenario.a), {}, content_type="application/json").status_code == 405
    with subtests.test("anonymous"):
        response = Client().get(url(scenario.a), {"q": "contrato"})
        assert response.status_code in {401, 403} and "results" not in response.json()
    with subtests.test("no membership in the organization"):
        assert login(scenario.outsider).get(url(scenario.a), {"q": "contrato"}).status_code == 404


def test_token_scopes_limit_the_tables(scenario):
    reader = token_client(scenario.member, [NOTE_READ])
    assert labels(reader.get(url(scenario.a), {"q": "contrato"})) == {"Contrato de locação"}
    writer = token_client(scenario.member, ["example_notes.note:create"])
    response = writer.get(url(scenario.a), {"q": "contrato"})
    assert response.status_code == 200 and response.json() == {"results": []}


def test_operation_is_in_the_openapi_schema(scenario):
    schema = login(scenario.member).get(reverse("api:openapi")).json()
    operation = schema["paths"]["/api/v1/o/{organization_id}/search/"]["get"]
    assert {parameter["name"] for parameter in operation["parameters"]} >= {"q", "limit"}
    assert "x-skeleton-undocumented" not in operation
