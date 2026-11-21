"""Templates of block pages (#619): every value is escaped and nothing is inline style or script."""

import re
from pathlib import Path

from django.template.loader import render_to_string
from django.utils import translation

from skeleton.common.page_blocks import validate_page

PAGES = Path(__file__).resolve().parents[2] / "skeleton" / "web" / "templates" / "skeleton" / "web" / "pages"
HOSTILE = '<script>alert("x")</script>'


def render(document, kind="platform_home"):
    blocks = validate_page(document, kind=kind)["blocks"]
    with translation.override("pt-br"):
        return render_to_string("skeleton/web/pages/blocks.html", {"blocks": blocks})


def test_every_text_is_escaped():
    html = render(
        {
            "version": 1,
            "blocks": [
                {"type": "hero", "title": HOSTILE, "subtitle": HOSTILE, "login_button": True},
                {"type": "text", "heading": HOSTILE, "body": f"{HOSTILE}\n\nSegundo <b>parágrafo</b>"},
                {"type": "contact", "address": HOSTILE, "hours": "9h", "email": "", "phone": ""},
                {"type": "links", "heading": HOSTILE, "items": [{"label": HOSTILE, "url": "/a"}]},
            ],
        }
    )
    assert "<script>" not in html and "<b>" not in html
    assert html.count("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;") >= 6
    assert "<p>Segundo &lt;b&gt;parágrafo&lt;/b&gt;</p>" in html  # Paragraphs split on blank lines.
    assert 'href="/accounts/login/"' in html  # The hero button leads to the login.


def test_external_links_get_rel_and_local_links_do_not():
    html = render(
        {
            "version": 1,
            "blocks": [
                {
                    "type": "links",
                    "items": [{"label": "Fora", "url": "https://exemplo.test/x"}, {"label": "Dentro", "url": "/join/"}],
                }
            ],
        }
    )
    assert 'href="https://exemplo.test/x" rel="noopener noreferrer"' in html
    assert '<a href="/join/">Dentro</a>' in html


def test_page_templates_have_no_inline_style_or_script():
    for path in sorted(PAGES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<script|<style|\sstyle=|\son[a-z]+=", text), path.name
        assert "|safe" not in text and "autoescape off" not in text, path.name
