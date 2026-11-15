"""The error templates render without any context, outside the web URLs (#473)."""

import re
from pathlib import Path

import pytest
from django.template.loader import render_to_string
from django.test import override_settings
from django.utils import translation

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "skeleton" / "web" / "templates"
PAGES = {
    "400.html": "Pedido inválido",
    "403.html": "Acesso não permitido",
    "403_csrf.html": "A página expirou",
    "404.html": "Página não encontrada",
    "500.html": "Algo deu errado do nosso lado",
}


@pytest.mark.parametrize(("name", "title"), PAGES.items())
def test_error_page_renders_without_context_or_url_tags(name, title):
    for path in (TEMPLATES / name, TEMPLATES / "skeleton" / "web" / "error_base.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"{%\s*url\b", text), path
    with translation.override("pt-br"), override_settings(ROOT_URLCONF="skeleton.setup.urls"):
        html = render_to_string(name)
    assert f"<h1>{title}</h1>" in html and 'href="/"' in html and "Ir para o início" in html
    assert 'name="robots" content="noindex"' in html


def test_csrf_page_offers_to_sign_in_again():
    with translation.override("pt-br"):
        html = render_to_string("403_csrf.html")
    assert 'href="/accounts/login/"' in html and "Entrar novamente" in html
