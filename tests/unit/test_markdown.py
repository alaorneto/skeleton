"""The closed Markdown dialect (#824): pure, no database.

``tests/fixtures/markdown/corpus.json`` is shared with the browser editor
(``frontend/tests``): both parsers must turn each entry into the same HTML.
"""

import json
import re
from pathlib import Path

import pytest
from django.template import Context, Template

from skeleton.common.markdown import (
    FEATURES,
    LINK_REL,
    MAX_SOURCE_LENGTH,
    MarkdownError,
    MarkdownTooLong,
    is_allowed_link,
    render_html,
    to_plain_text,
    validate_features,
)

CORPUS = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "markdown" / "corpus.json").read_text("utf-8"))
ATTACKS = (
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '<a href="javascript:alert(1)">x</a>',
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](java&#x73;cript:alert(1))",
    "[x](&#106;avascript:alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD4=)",
    "[x](vbscript:msgbox(1))",
    "[x](//evil.test/a)",
    "[x](/\\evil.test)",
    "[x](https:/evil.test)",
    '[x](https://ok.test "a\\" onmouseover=\\"alert(1)")',
    "![x](https://evil.test/pixel.png)",
    "<https://evil.test>",
    "[x]: javascript:alert(1)\n\n[x]",
    "`<script>`",
    "**<b onclick=alert(1)>x</b>**",
)


@pytest.mark.parametrize("case", CORPUS, ids=[case["name"] for case in CORPUS])
def test_corpus(case):
    assert render_html(case["markdown"], features=case.get("features")) == case["html"]


@pytest.mark.parametrize("source", ATTACKS)
def test_nothing_typed_becomes_active_markup(source):
    html = render_html(source)
    assert not re.search(r"<(?!/?(p|br|strong|em|u|s|code|a|ul|ol|li|h[1-3]|blockquote)\b)", html), html
    assert "<img" not in html
    assert "onerror=" not in html.replace("onerror=alert(1)&gt;", "")
    for href in re.findall(r'href="([^"]*)"', html):
        assert href.startswith("https://") or (href.startswith("/") and not href.startswith("//")), href
    for anchor in re.findall(r"<a [^>]*>", html):
        assert re.fullmatch(r'<a( (href|title|rel)="[^"]*")+>', anchor), anchor


def test_links_carry_rel_and_keep_their_label_when_refused():
    assert render_html("[a](https://exemplo.test)") == (
        f'<p><a href="https://exemplo.test" rel="{LINK_REL}">a</a></p>\n'
    )
    assert render_html("[rótulo](javascript:x)").startswith("<p>[rótulo]")


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://exemplo.test", True),
        ("HTTPS://exemplo.test/a", True),
        ("/o/x/", True),
        ("https://", False),
        ("http://exemplo.test", False),
        ("//evil.test", False),
        ("/\\evil.test", False),
        ("mailto:a@b.test", False),
        ("javascript:alert(1)", False),
        ("https://exemplo.test/\na", False),
        ("relativo", False),
        ("", False),
    ],
)
def test_link_targets(url, allowed):
    assert is_allowed_link(url) is allowed


def test_features_are_canonical_and_closed():
    assert validate_features(None) == FEATURES
    assert validate_features(["link", "emphasis"]) == ("emphasis", "link")
    assert validate_features([]) == ()
    with pytest.raises(MarkdownError, match="Unknown"):
        validate_features(["emphasis", "table"])
    with pytest.raises(MarkdownError, match="Repeated"):
        validate_features(["emphasis", "emphasis"])
    with pytest.raises(MarkdownError, match="sequence"):
        validate_features("emphasis")


def test_each_feature_is_off_unless_requested():
    samples = {
        "emphasis": ("**a**", "<strong>"),
        "underline": ("++a++", "<u>"),
        "strike": ("~~a~~", "<s>"),
        "code": ("`a`", "<code>"),
        "heading": ("# a", "<h1>"),
        "lists": ("- a", "<ul>"),
        "quote": ("> a", "<blockquote>"),
        "link": ("[a](https://exemplo.test)", "<a "),
    }
    assert set(samples) == set(FEATURES)
    for feature, (source, tag) in samples.items():
        assert tag in render_html(source, features=[feature]), feature
        others = [name for name in FEATURES if name != feature]
        assert tag not in render_html(source, features=others), feature


def test_headings_stop_at_level_three():
    assert render_html("###### a") == "<h3>a</h3>\n"


def test_length_limit():
    assert render_html("a" * MAX_SOURCE_LENGTH).startswith("<p>")
    with pytest.raises(MarkdownTooLong):
        render_html("a" * (MAX_SOURCE_LENGTH + 1))
    with pytest.raises(MarkdownError):
        render_html(None)


def test_deep_nesting_stays_bounded():
    html = render_html("> " * 200 + "a")
    assert html.count("<blockquote>") <= 20


def test_plain_text():
    source = "# Título\n- **a** ++b++\n- `c`\n\nlinha\noutra [l](https://exemplo.test)"
    assert to_plain_text(source) == "Título\na b\nc\nlinha outra l"
    assert to_plain_text("**a**", features=[]) == "**a**"
    assert to_plain_text("") == ""


def _template(source, **context):
    return Template("{% load skeleton_markdown %}" + source).render(Context(context))


def test_markdown_filter():
    assert _template("{{ v|markdown }}", v="**a** <b>") == (
        '<div class="markdown"><p><strong>a</strong> &lt;b&gt;</p>\n</div>'
    )
    assert _template('{{ v|markdown:"lists" }}', v="**a**") == '<div class="markdown"><p>**a**</p>\n</div>'
    assert _template("{{ v|markdown }}", v="") == ""
    assert _template("{{ v|markdown }}", v=None) == ""


def test_markdown_filter_shows_a_too_long_value_as_escaped_text():
    value = "<b>x</b>\n" + "a" * MAX_SOURCE_LENGTH
    html = _template("{{ v|markdown }}", v=value)
    assert html.startswith('<div class="markdown">&lt;b&gt;x&lt;/b&gt;<br>')


def test_markdown_filter_refuses_an_unknown_feature():
    with pytest.raises(MarkdownError):
        _template('{{ v|markdown:"tables" }}', v="a")


def test_markdown_plain_filter_is_autoescaped():
    assert _template("{{ v|markdown_plain }}", v="**<b>** x") == "&lt;b&gt; x"
