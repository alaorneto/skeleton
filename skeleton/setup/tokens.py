"""Single-use setup token: protects the first access of a fresh instance from being hijacked.

The plaintext exists only when it is issued: it is printed once to the log and written to a
0600 file next to the instance SQLite; the store keeps only its SHA-256 digest.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

TOKEN_LIFETIME = timedelta(hours=24)
TOKEN_BYTES = 24


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_file(store) -> Path:
    return store.path.with_name("setup-token")


def issue_token(store, *, now: datetime | None = None, stream=None) -> str | None:
    """Issue a token unless an unused, unexpired one exists; returns the new plaintext."""
    now = now or datetime.now(UTC)
    if store.has_open_setup_token(now=now):
        return None
    token = secrets.token_urlsafe(TOKEN_BYTES)
    if not store.add_setup_token(digest(token), now=now, expires_at=now + TOKEN_LIFETIME):
        return None  # Another worker issued it first.
    path = token_file(store)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        path.unlink()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    print(  # noqa: T201 - the one place the operator learns the token
        f"[skeleton setup] Token de primeiro acesso (uso único, vale 24 h): {token}\n"
        f"[skeleton setup] Também gravado em {path} (0600). Abra /setup/ e informe o token.",
        file=stream or sys.stderr,
        flush=True,
    )
    return token


def consume_token(store, candidate: str, *, now: datetime | None = None) -> bool:
    """True exactly once for a valid token; wrong, reused or expired tokens are refused."""
    if not isinstance(candidate, str) or not 10 <= len(candidate) <= 200:
        return False
    now = now or datetime.now(UTC)
    accepted = store.consume_setup_token(digest(candidate.strip()), now=now)
    if accepted:
        path = token_file(store)
        if path.exists():
            path.unlink()
    return accepted
