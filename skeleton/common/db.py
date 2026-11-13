"""Small PostgreSQL session helpers shared by job handlers."""

from __future__ import annotations

from django.db import connections

LOCK_TIMEOUT_MS = 5000


def bound_lock_waits(*, using: str = "default", milliseconds: int = LOCK_TIMEOUT_MS) -> None:
    """Make lock waits of the current transaction fail after ``milliseconds`` (#295).

    Without it a statement queues behind another transaction's row lock with no limit. The
    setting is local to the transaction (``set_config(..., is_local => true)``) and ends with it.
    The value is an integer formatted here and sent as a query parameter, never caller text.
    """
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int) or not 1 <= milliseconds <= 600_000:
        raise ValueError("milliseconds must be an integer between 1 and 600000.")
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [f"{milliseconds}ms"])
