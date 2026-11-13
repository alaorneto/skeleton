"""Mention tokens (#188): parsing, token building and escaped rendering, no database."""

from uuid import UUID

from skeleton.comments.mentions import mentioned_ids, token
from skeleton.comments.text import linkify

A = UUID("11111111-1111-4111-8111-111111111111")
B = UUID("22222222-2222-4222-8222-222222222222")


def test_tokens_are_parsed_once_in_order():
    body = f"oi @[Ana]({A}) e @[Bia]({B}), de novo @[Ana Paula]({A}) @[x](nope) @[]({B}) [Ana]({A})"
    assert mentioned_ids(body) == [A, B]
    assert mentioned_ids("sem menções") == []


def test_token_label_never_breaks_the_syntax():
    assert token("Ana [a](b)\n<x>", A) == f"@[Ana abx]({A})"
    assert token("", A) == f"@[pessoa]({A})"
    assert mentioned_ids(token("Ana ]) <b>", B)) == [B]


def test_rendering_escapes_the_label_and_keeps_the_sysid_out_of_the_text():
    html = linkify(f"oi @[Ana & <b>Bia</b>]({A})")
    assert "<b>" not in html
    assert f'<span class="mention" data-mention="{A}">@Ana &amp; &lt;b&gt;Bia&lt;/b&gt;</span>' in html
