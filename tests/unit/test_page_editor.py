"""The shared SSR editor of block pages (#619) over a fake store: POSTs with RequestFactory, no database."""

import copy

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.middleware.csrf import _get_new_csrf_string, _mask_cipher_secret
from django.test import RequestFactory
from django.utils import translation

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.common.page_blocks import PageState, validate_page
from skeleton.web import page_editor
from skeleton.web.page_editor import PageEditor, editor_urls

SECRET = _get_new_csrf_string()


class FakeStore:
    def __init__(self, blocks=(), published=False):
        self.state = PageState({"version": 1, "blocks": list(blocks)}, published, 1)
        self.saves = 0

    def load(self):
        return PageState(copy.deepcopy(self.state.document), self.state.published, self.state.revision)

    def save(self, *, document, published, expected_revision):
        if expected_revision != self.state.revision:
            raise ConcurrentUpdate("stale")
        document = validate_page(document, kind="platform_home", allow_empty=not published)
        self.state = PageState(document, published, self.state.revision + 1)
        self.saves += 1
        return self.state


def preview(request, state):
    return HttpResponse("preview:" + ",".join(block["type"] for block in state.blocks))


def make_editor(store):
    return PageEditor(
        kind="platform_home",
        store=store,
        url_name="test-page",
        base_template="skeleton/web/base.html",
        title="Página de teste",
        preview=preview,
    )


STORE: dict = {}


def view(request, operation, index=None):
    return page_editor.dispatch(request, make_editor(STORE["store"]), operation, index=index)


def _urlpatterns():
    from config.urls import urlpatterns as project

    return editor_urls("page/", "test-page", view) + list(project)


urlpatterns = _urlpatterns()


@pytest.fixture
def store(settings):
    settings.ROOT_URLCONF = __name__
    STORE["store"] = FakeStore([{"type": "text", "heading": "Um", "body": "Primeiro"}])
    yield STORE["store"]
    STORE.clear()


def call(method, url, data=None, *, csrf=True, operation, index=None):
    factory = RequestFactory()
    data = dict(data or {})
    if method == "post":
        if csrf:
            data["csrfmiddlewaretoken"] = _mask_cipher_secret(SECRET)
        request = factory.post(url, data)
        if csrf:
            request.COOKIES["csrftoken"] = SECRET
    else:
        request = factory.get(url, data)
    request.user = AnonymousUser()
    request.session = {}
    with translation.override("pt-br"):
        response = view(request, operation, index)
    if hasattr(response, "render"):
        response.render()
    return response


def test_add_edit_move_and_delete_with_cas(store):
    assert call("get", "/page/", operation="index").status_code == 200
    form = call("get", "/page/blocks/new/?type=hero", operation="add")
    assert form.status_code == 200 and 'name="title"' in form.content.decode()
    added = call(
        "post", "/page/blocks/new/?type=hero", {"title": "Olá", "login_button": "on", "revision": 1}, operation="add"
    )
    assert added.status_code == 302 and [block["type"] for block in store.state.blocks] == ["text", "hero"]
    moved = call("post", "/page/blocks/1/move/", {"direction": "up", "revision": 2}, operation="move", index=1)
    assert moved.status_code == 302 and [block["type"] for block in store.state.blocks] == ["hero", "text"]
    edited = call(
        "post",
        "/page/blocks/1/",
        {"heading": "Novo", "body": "Linha\r\n\r\nOutra", "revision": 3},
        operation="edit",
        index=1,
    )
    assert edited.status_code == 302 and store.state.blocks[1]["body"] == "Linha\n\nOutra"
    links = call(
        "post",
        "/page/blocks/new/?type=links",
        {"heading": "", "items": "Blog | https://exemplo.test/blog\r\nEntrar | /accounts/login/", "revision": 4},
        operation="add",
    )
    assert links.status_code == 302 and store.state.blocks[2]["items"][1] == {
        "label": "Entrar",
        "url": "/accounts/login/",
    }
    assert call("get", "/page/blocks/0/delete/", operation="delete", index=0).status_code == 200
    deleted = call("post", "/page/blocks/0/delete/", {"revision": 5}, operation="delete", index=0)
    assert deleted.status_code == 302 and [block["type"] for block in store.state.blocks] == ["text", "links"]
    published = call("post", "/page/publish/", {"published": "on", "revision": 6}, operation="publish")
    assert published.status_code == 302 and store.state.published
    assert call("get", "/page/preview/", operation="preview").content == b"preview:text,links"


def test_stale_revision_answers_409_without_writing(store, subtests):
    cases = {
        "add": ("/page/blocks/new/?type=text", {"body": "x", "revision": 0}, None),
        "edit": ("/page/blocks/0/", {"body": "x", "revision": 0}, 0),
        "move": ("/page/blocks/0/move/", {"direction": "down", "revision": 0}, 0),
        "delete": ("/page/blocks/0/delete/", {"revision": 0}, 0),
        "publish": ("/page/publish/", {"published": "on", "revision": 0}, None),
    }
    for operation, (url, data, index) in cases.items():
        with subtests.test(operation):
            response = call("post", url, data, operation=operation, index=index)
            assert response.status_code == 409
            assert "Esta página foi alterada por outra pessoa" in response.content.decode()
    assert store.saves == 0


def test_validation_error_answers_400_with_error_summary(store, subtests):
    cases = {
        "javascript link": (
            "/page/blocks/new/?type=links",
            {"items": "X | javascript:alert(1)", "revision": 1},
            "add",
            None,
        ),
        "scheme-relative link": (
            "/page/blocks/new/?type=links",
            {"items": "X | //evil.test", "revision": 1},
            "add",
            None,
        ),
        "invalid email": ("/page/blocks/new/?type=contact", {"email": "nope", "revision": 1}, "add", None),
        "empty body": ("/page/blocks/0/", {"body": "  ", "revision": 1}, "edit", 0),
        "title too long": ("/page/blocks/new/?type=hero", {"title": "x" * 121, "revision": 1}, "add", None),
    }
    for name, (url, data, operation, index) in cases.items():
        with subtests.test(name):
            response = call("post", url, data, operation=operation, index=index)
            assert response.status_code == 400
            assert "error-summary" in response.content.decode()
    assert store.saves == 0


def test_publishing_an_empty_page_is_refused(store):
    assert call("post", "/page/blocks/0/delete/", {"revision": 1}, operation="delete", index=0).status_code == 302
    response = call("post", "/page/publish/", {"published": "on", "revision": 2}, operation="publish")
    assert response.status_code == 400 and not store.state.published


def test_nothing_works_without_csrf(store, subtests):
    cases = {
        "add": ("/page/blocks/new/?type=text", {"body": "x", "revision": 1}, None),
        "edit": ("/page/blocks/0/", {"body": "x", "revision": 1}, 0),
        "move": ("/page/blocks/0/move/", {"direction": "down", "revision": 1}, 0),
        "delete": ("/page/blocks/0/delete/", {"revision": 1}, 0),
        "publish": ("/page/publish/", {"published": "on", "revision": 1}, None),
    }
    for operation, (url, data, index) in cases.items():
        with subtests.test(operation):
            assert call("post", url, data, csrf=False, operation=operation, index=index).status_code == 403
    assert store.saves == 0


def test_unknown_fields_types_and_indexes_are_refused(store, subtests):
    from django.http import Http404

    with subtests.test("unknown field"):
        response = call(
            "post", "/page/blocks/0/", {"body": "x", "style": "a", "revision": 1}, operation="edit", index=0
        )
        assert response.status_code == 400
    with subtests.test("type outside the kind"), pytest.raises(Http404):
        call("get", "/page/blocks/new/?type=widget", operation="add")
    with subtests.test("missing block"), pytest.raises(Http404):
        call("get", "/page/blocks/9/", operation="edit", index=9)
    with subtests.test("move past the end"):
        assert (
            call(
                "post", "/page/blocks/0/move/", {"direction": "up", "revision": 1}, operation="move", index=0
            ).status_code
            == 400
        )
    with subtests.test("GET on publish"):
        assert call("get", "/page/publish/", operation="publish").status_code == 405
    assert store.saves == 0
