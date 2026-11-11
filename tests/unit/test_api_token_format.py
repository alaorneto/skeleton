"""Token text, digest and scope grammar (#45), without the database."""

import hashlib

import pytest
from django.core.exceptions import ValidationError

from skeleton.integrations.tokens import scopes as scope_module
from skeleton.integrations.tokens.scopes import register_token_scope, table_scopes, validate_scopes
from skeleton.integrations.tokens.services import TOKEN_PATTERN, digest


def test_token_pattern_and_digest():
    text = "skt_0a1b2c3d_" + "A" * 43
    assert TOKEN_PATTERN.match(text).groups() == ("0a1b2c3d", "A" * 43)
    for wrong in ("skt_0A1B2C3D_" + "A" * 43, "skt_0a1b2c3d_" + "A" * 42, "Bearer skt_0a1b2c3d_" + "A" * 43, ""):
        assert TOKEN_PATTERN.match(wrong) is None
    assert digest("secret") == hashlib.sha256(b"secret").hexdigest()


def test_scope_grammar(monkeypatch):
    monkeypatch.setattr(scope_module, "_named", {})
    register_token_scope("files:read", "Ler anexos")
    register_token_scope("calendar.feed", "Feed", feed=True)
    exposed = table_scopes()
    assert all(scope.rsplit(":", 1)[1] in scope_module.TABLE_ACTIONS for scope in exposed)
    if exposed:
        assert validate_scopes([exposed[0], exposed[0], "files:read"], feed=False) == sorted({exposed[0], "files:read"})
    assert validate_scopes(["calendar.feed"], feed=True) == ["calendar.feed"]
    for bad, feed in ((["calendar.feed"], False), (["files:read"], True), ([1], False), (["x"] * 51, False)):
        with pytest.raises(ValidationError):
            validate_scopes(bad, feed=feed)
    with pytest.raises(ValueError):
        register_token_scope("Bad Scope", "x")
    with pytest.raises(ValueError):
        register_token_scope("files:read", "Outra descrição")
