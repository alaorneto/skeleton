"""Error pages in Portuguese with a way back, and the same 404 for missing and foreign (#473)."""

import uuid

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.web.errors import error_response

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Error-pages-password!473"


def logged(slug):
    actor, organization = bootstrap_identity(
        email=f"{slug}@example.test", organization=slug, name=f"Org {slug}", password=PASSWORD
    )
    client = Client(raise_request_exception=False)
    assert client.post("/accounts/login/", {"login": actor.email, "password": PASSWORD}).status_code == 302
    return client, organization


@override_settings(DEBUG=False)
def test_django_error_pages_are_the_system_ones(subtests):
    client, organization = logged("errors-a")
    with subtests.test("404"):
        response = client.get("/nao-existe/qualquer/")
        html = response.content.decode()
        assert response.status_code == 404 and "<h1>Página não encontrada</h1>" in html
        assert "nao-existe" not in html and "site.css" in html
    with subtests.test("csrf"):
        anonymous = Client(enforce_csrf_checks=True)
        response = anonymous.post("/accounts/login/", {"login": "x@example.test", "password": "y"})
        assert response.status_code == 403 and "<h1>A página expirou</h1>" in response.content.decode()
    with subtests.test("403"):
        from skeleton.accounts.services import create_identity

        person = create_identity(email="errors-plain@example.test", password=PASSWORD)
        plain = Client(raise_request_exception=False)
        plain.force_login(person)
        response = plain.get("/platform/")  # PermissionDenied: not a platform administrator.
        assert response.status_code == 403 and "<h1>Acesso não permitido</h1>" in response.content.decode()
    with subtests.test("500"):
        from django.views.defaults import server_error

        response = server_error(RequestFactory().get("/"))
        html = response.content.decode()
        assert response.status_code == 500 and "<h1>Algo deu errado do nosso lado</h1>" in html
        assert "Traceback" not in html


@override_settings(DEBUG=False)
def test_missing_and_foreign_records_answer_the_same_404():
    client, organization = logged("errors-b")
    _, foreign = bootstrap_identity(
        email="errors-c@example.test", organization="errors-c", name="Foreign", password=PASSWORD
    )
    from skeleton.access.models import Role
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:error-pages", organization_id=foreign.pk):
        foreign_role = Role.objects.filter(organization=foreign).first()
    assert foreign_role is not None
    pages = []
    for sysid in (foreign_role.pk, uuid.uuid4()):
        response = client.get(
            reverse("web:access-edit", kwargs={"organization_id": organization.pk, "resource": "roles", "sysid": sysid})
        )
        assert response.status_code == 404
        pages.append(response.content)
    assert pages[0] == pages[1]
    assert client.get(f"/o/{foreign.pk}/").content == client.get(f"/o/{uuid.uuid4()}/").content


def _request(referer=None, host="testserver"):
    request = RequestFactory().get("/o/x/", HTTP_HOST=host, **({"HTTP_REFERER": referer} if referer else {}))
    request.user = AnonymousUser()
    return request


def test_error_response_titles_and_way_back(subtests):
    with subtests.test("conflict with a referer of this host"):
        response = error_response(_request("http://testserver/o/x/tables/a/"), "Mudou.", status=409)
        html = response.content.decode()
        assert response.status_code == 409 and "<h1>Os dados mudaram</h1>" in html
        assert 'href="http://testserver/o/x/tables/a/"' in html and "Voltar e recarregar" in html
        assert 'role="alert"' in html and "Mudou." in html
    with subtests.test("referer of another host is ignored"):
        html = error_response(_request("https://evil.example/phish"), "x", status=400).content.decode()
        assert "evil.example" not in html and "Voltar" not in html and "Ir para o início" in html
    with subtests.test("no referer"):
        html = error_response(_request(), "x", status=429).content.decode()
        assert "<h1>Muitas tentativas</h1>" in html and "Voltar" not in html
    with subtests.test("explicit way back"):
        html = error_response(_request(), "x", status=400, back_url="/o/x/", back_label="Voltar à lista").content
        assert b'href="/o/x/"' in html and "Voltar à lista".encode() in html
    for status, title in (
        (403, "Acesso não permitido"),
        (404, "Página não encontrada"),
        (413, "Arquivo grande demais"),
        (500, "Não foi possível concluir"),
    ):
        with subtests.test(status=status):
            assert f"<h1>{title}</h1>" in error_response(_request(), "x", status=status).content.decode()
