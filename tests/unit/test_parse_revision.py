"""Unified ``If-Match`` parsing and LIKE escaping helpers (#912)."""

from __future__ import annotations

import pytest

from skeleton.api.errors import BoundaryError, parse_revision
from skeleton.system.search import _escape_like


def test_parse_revision_enforces_consistent_rules_and_optional_zero():
    assert parse_revision('"1"') == 1
    assert parse_revision('"42"') == 42
    assert parse_revision('"0"', allow_zero=True) == 0
    assert parse_revision('"3-5"', composite=True) == (3, 5)

    with pytest.raises(BoundaryError) as missing:
        parse_revision(None)
    assert missing.value.status_code == 428

    for bad in ('"0"', '"-1"', "1", '""', '"01"', '"99999999999999999999"'):
        with pytest.raises(BoundaryError) as invalid:
            parse_revision(bad)
        assert invalid.value.status_code == 400

    with pytest.raises(BoundaryError):
        parse_revision('"0-1"', composite=True, allow_zero=True)


def test_escape_like_neutralizes_wildcards_and_escape_char():
    assert _escape_like("50%") == r"50\%"
    assert _escape_like("a_b") == r"a\_b"
    assert _escape_like(r"c\d%e_f") == r"c\\d\%e\_f"
