"""Validation of block pages (#619): pure, no database."""

import copy
import json

import pytest
from django.core.exceptions import ValidationError

from skeleton.common.page_blocks import (
    BLOCK_TYPES,
    DEFAULT_ORGANIZATION_HOME,
    EMPTY_PAGE,
    FIELDS,
    MAX_BYTES,
    PAGE_KINDS,
    PageState,
    validate_page,
)

EXAMPLE = {
    "version": 1,
    "blocks": [
        {"type": "text", "heading": "Quem somos", "body": "Parágrafo um.\n\nParágrafo dois."},
        {"type": "contact", "email": "contato@exemplo.test", "phone": "+55 11 5555-0000", "address": "", "hours": "9h"},
        {"type": "links", "heading": "Links", "items": [{"label": "Blog", "url": "https://exemplo.test/blog"}]},
        {"type": "apps", "heading": ""},
        {"type": "tasks", "heading": ""},
        {"type": "notifications", "heading": ""},
        {"type": "widget", "name": "example_notes.recent"},
    ],
}
PUBLIC = {
    "version": 1,
    "blocks": [
        {
            "type": "hero",
            "title": "Empresa Exemplo",
            "subtitle": "Atendimento de segunda a sexta",
            "login_button": True,
        },
        {"type": "text", "heading": "", "body": "Texto."},
    ],
}
WIDGETS = frozenset({"example_notes.recent"})


def errors_of(document, *, kind="organization_home", widgets=WIDGETS, **options):
    with pytest.raises(ValidationError) as caught:
        validate_page(document, kind=kind, known_widgets=widgets, **options)
    return caught.value.message_dict


def with_block(block, kind="organization_home"):
    return {"version": 1, "blocks": [block]}


def test_the_example_of_the_design_is_valid_and_normalized():
    result = validate_page(copy.deepcopy(EXAMPLE), kind="organization_home", known_widgets=WIDGETS)
    assert [block["type"] for block in result["blocks"]] == [block["type"] for block in EXAMPLE["blocks"]]
    assert result["blocks"][0]["body"] == "Parágrafo um.\n\nParágrafo dois."
    public = validate_page({"version": 1, "blocks": [{"type": "hero", "title": "  Olá  "}]}, kind="platform_home")
    assert public["blocks"][0] == {"type": "hero", "title": "Olá", "subtitle": "", "login_button": True}
    assert validate_page(copy.deepcopy(PUBLIC), kind="organization_public")["blocks"][0]["login_button"] is True
    assert validate_page(copy.deepcopy(DEFAULT_ORGANIZATION_HOME), kind="organization_home")["blocks"]


def test_every_type_has_fields_and_every_kind_lists_known_types():
    assert set(PAGE_KINDS) == set(BLOCK_TYPES)
    for types in BLOCK_TYPES.values():
        assert set(types) <= set(FIELDS)
    assert EMPTY_PAGE == {"version": 1, "blocks": []}
    assert PageState().blocks == [] and PageState().revision == 0


def test_refused_documents_name_the_block_and_the_field(subtests):
    long_body = "x" * 4001
    cases = {
        "unknown type": (with_block({"type": "html", "body": "<b>"}), "blocks.0.type"),
        "unknown key": (with_block({"type": "text", "body": "a", "style": "color:red"}), "blocks.0.style"),
        "unknown top key": ({"version": 1, "blocks": [{"type": "text", "body": "a"}], "css": ""}, "css"),
        "type outside the kind": (with_block({"type": "hero", "title": "x"}), "blocks.0.type"),
        "no block": ({"version": 1, "blocks": []}, "blocks"),
        "21 blocks": ({"version": 1, "blocks": [{"type": "text", "body": "a"}] * 21}, "blocks"),
        "version 2": ({"version": 2, "blocks": [{"type": "text", "body": "a"}]}, "version"),
        "version as text": ({"version": "1", "blocks": [{"type": "text", "body": "a"}]}, "version"),
        "http url": (
            with_block({"type": "links", "items": [{"label": "a", "url": "http://x.test/"}]}),
            "blocks.0.items.0.url",
        ),
        "javascript url": (
            with_block({"type": "links", "items": [{"label": "a", "url": "javascript:alert(1)"}]}),
            "blocks.0.items.0.url",
        ),
        "scheme-relative url": (
            with_block({"type": "links", "items": [{"label": "a", "url": "//evil.test/x"}]}),
            "blocks.0.items.0.url",
        ),
        "backslash url": (
            with_block({"type": "links", "items": [{"label": "a", "url": "/\\evil.test"}]}),
            "blocks.0.items.0.url",
        ),
        "https with backslash": (
            with_block({"type": "links", "items": [{"label": "a", "url": "https://x.test\\@evil.test"}]}),
            "blocks.0.items.0.url",
        ),
        "no links": (with_block({"type": "links", "items": []}), "blocks.0.items"),
        "11 links": (with_block({"type": "links", "items": [{"label": "a", "url": "/a"}] * 11}), "blocks.0.items"),
        "invalid email": (with_block({"type": "contact", "email": "not-an-email"}), "blocks.0.email"),
        "phone with letters": (with_block({"type": "contact", "phone": "0800 CALL"}), "blocks.0.phone"),
        "empty contact": (with_block({"type": "contact"}), "blocks.0"),
        "control character": (with_block({"type": "text", "heading": "a\x07b", "body": "x"}), "blocks.0.heading"),
        "newline in a line": (with_block({"type": "text", "heading": "a\nb", "body": "x"}), "blocks.0.heading"),
        "control character in body": (with_block({"type": "text", "body": "a\x00b"}), "blocks.0.body"),
        "empty body": (with_block({"type": "text", "body": "   "}), "blocks.0.body"),
        "long body": (with_block({"type": "text", "body": long_body}), "blocks.0.body"),
        "unknown widget": (with_block({"type": "widget", "name": "other.thing"}), "blocks.0.name"),
        "login_button as text": (
            {"version": 1, "blocks": [{"type": "hero", "title": "x", "login_button": "yes"}]},
            "blocks.0.login_button",
        ),
        "not a dict": ("text", "document"),
    }
    for name, (document, key) in cases.items():
        with subtests.test(name):
            kind = "platform_home" if name == "login_button as text" else "organization_home"
            assert key in errors_of(document, kind=kind)


def test_size_limit_is_checked_on_the_serialized_document():
    blocks = [{"type": "text", "heading": "", "body": "é" * 4000} for _ in range(5)]  # 2 bytes per character.
    document = {"version": 1, "blocks": blocks}
    assert len(json.dumps(document, ensure_ascii=False).encode()) > MAX_BYTES
    assert "document" in errors_of(document)


def test_local_paths_and_https_are_accepted_and_multiline_fields_keep_newlines():
    block = {
        "type": "links",
        "heading": "",
        "items": [
            {"label": "Entrar", "url": "/accounts/login/"},
            {"label": "Site", "url": "https://exemplo.test/a?b=1"},
        ],
    }
    assert validate_page(with_block(block), kind="platform_home")["blocks"][0]["items"][1]["url"].startswith("https")
    contact = {"type": "contact", "address": "Rua A, 1\nSão Paulo", "hours": "", "email": "", "phone": "(11) 5555-0000"}
    assert validate_page(with_block(contact), kind="platform_home")["blocks"][0]["address"] == "Rua A, 1\nSão Paulo"


def test_empty_document_is_allowed_only_when_asked():
    assert validate_page({"version": 1, "blocks": []}, kind="platform_home", allow_empty=True)["blocks"] == []
    with pytest.raises(ValueError):
        validate_page({"version": 1, "blocks": []}, kind="nope")
