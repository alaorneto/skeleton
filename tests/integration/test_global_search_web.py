"""Search page and header search box (#584)."""

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from skeleton.testing.registries import table_spec_override
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
KEY = "example_notes.note"


@pytest.fixture
def scenario():
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="global-search-web")
    scenario.locacao = scenario.create(scenario.a, owner=scenario.member, code="S-1", title="Contrato de locação")
    scenario.create(scenario.a, owner=scenario.member, code="S-2", title="Ação judicial trabalhista")
    scenario.create(scenario.a, owner=scenario.admin_a, code="S-3", title="Contrato confidencial", visibility="private")
    scenario.create(scenario.b, owner=scenario.admin_b, code="B-1", title="Contrato da outra organização")
    scenario.create(scenario.a, owner=scenario.member, code="S-9", title="<script>alert(1)</script> contrato")
    return scenario


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def page(organization):
    return reverse("web:search", kwargs={"organization_id": organization.pk})


def test_search_page(scenario, subtests):
    member = login(scenario.member)
    url = page(scenario.a)
    record_url = reverse(
        "web:record", kwargs={"organization_id": scenario.a.pk, "table_key": KEY, "sysid": scenario.locacao.pk}
    )
    with subtests.test("empty form"):
        response = member.get(url)
        content = response.content.decode()
        assert response.status_code == 200 and 'name="q"' in content and "Nenhum resultado." not in content
    with subtests.test("results grouped by table, linked to the record"):
        response = member.get(url, {"q": "contr"})
        content = response.content.decode()
        assert response.status_code == 200
        assert "Anotações" in content and f'href="{record_url}"' in content and "Contrato de locação" in content
        assert member.get(record_url).status_code == 200
    with subtests.test("more results than the limit"):
        content = member.get(url, {"q": "contr", "limit": "1"}).content.decode()
        records = reverse("web:records", kwargs={"organization_id": scenario.a.pk, "table_key": KEY})
        assert "Ver todos os resultados" in content and f'href="{records}?q=contr"' in content
    with subtests.test("no result"):
        response = member.get(url, {"q": "inexistente"})
        assert response.status_code == 200 and "Nenhum resultado." in response.content.decode()
    for params in ({"q": "c"}, {"q": ""}, {"q": "contr", "x": "1"}):
        with subtests.test("invalid", params=params):
            response = member.get(url, params)
            content = response.content.decode()
            assert response.status_code == 400
            assert "Use de 2 a 200 caracteres." in content and 'aria-invalid="true"' in content
            assert f'value="{params["q"]}"' in content
    with subtests.test("other organization and private of another person"):
        content = member.get(url, {"q": "contrato"}).content.decode()
        assert "Contrato da outra organização" not in content and "Contrato confidencial" not in content
    with subtests.test("titles are escaped"):
        content = member.get(url, {"q": "contrato"}).content.decode()
        assert "&lt;script&gt;alert(1)&lt;/script&gt; contrato" in content and "<script>alert(1)" not in content
    with subtests.test("default deny"):
        content = login(scenario.denied).get(url, {"q": "contrato"}).content.decode()
        assert "Nenhum resultado." in content and "Contrato de locação" not in content
    with subtests.test("anonymous goes to the login"):
        response = Client().get(url, {"q": "contrato"})
        assert response.status_code == 302 and reverse("account_login") in response["Location"]
    with subtests.test("no membership"):
        assert login(scenario.outsider).get(url, {"q": "contrato"}).status_code == 404


def test_header_search_box(scenario, subtests):
    member = login(scenario.member)
    dashboard = reverse("web:dashboard", kwargs={"organization_id": scenario.a.pk})
    with subtests.test("organization pages carry the box"):
        content = member.get(dashboard).content.decode()
        assert 'class="header-search"' in content and f'action="{page(scenario.a)}"' in content
    with subtests.test("home page does not"):
        assert 'class="header-search"' not in member.get(reverse("web:home")).content.decode()
    with subtests.test("no marked table, no box"):
        with table_spec_override(KEY, global_search=None):
            assert 'class="header-search"' not in member.get(dashboard).content.decode()
    with subtests.test("the box costs no query"):
        member.get(dashboard)  # Warm the per-process caches.
        with CaptureQueriesContext(connection) as marked:
            member.get(dashboard)
        with table_spec_override(KEY, global_search=None):
            with CaptureQueriesContext(connection) as unmarked:
                member.get(dashboard)
        assert len(marked) == len(unmarked)
