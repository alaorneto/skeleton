"""Comment bodies (#187): plain text validation and escaped autolinks, no database."""

import pytest
from django.core.exceptions import ValidationError

from skeleton.comments.text import clean_body, linkify


def test_body_is_trimmed_and_bounded():
    assert clean_body("  olá\r\nmundo  ") == "olá\nmundo"
    assert len(clean_body("x" * 5000)) == 5000
    for bad in ("", "   ", "x" * 5001, "a\x00b", "a\x1bb", None, 3):
        with pytest.raises(ValidationError):
            clean_body(bad)


def test_html_is_escaped_and_never_becomes_markup():
    html = linkify("<script>alert(1)</script> <img src=x onerror=alert(1)> **negrito**")
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "**negrito**" in html


def test_only_http_urls_become_links():
    html = linkify('veja https://example.test/a?b=1&c=2. e javascript:alert(1) ou http://x.test/"onmouseover=1')
    assert '<a href="https://example.test/a?b=1&amp;c=2" rel="nofollow noopener noreferrer" target="_blank">' in html
    assert html.count("<a ") == 2 and "javascript:alert(1)" in html and 'href="javascript' not in html
    assert 'href="http://x.test/&quot;onmouseover=1"' in html and " onmouseover" not in html
