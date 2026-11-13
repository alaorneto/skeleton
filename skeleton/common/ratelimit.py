"""Fixed-window rate limiting in the shared cache (#48).

``hit(key, limit=..., window_s=...)`` counts one event in the current window and says whether
it is still within the limit. The window is ``floor(now / window_s)``: cheap, one ``add`` and
one ``incr`` per event, at the cost of allowing up to twice the limit across a window edge.
It needs a cache shared by every worker (Redis in production; ``skeleton.W029`` warns about
LocMem with ``DEBUG=False``). A cache failure lets the request through: the limiter is a
brake on abuse, never the authorization.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from django.core.cache import cache

PREFIX = "skeleton:rl:"


@dataclass(frozen=True, slots=True)
class RateResult:
    allowed: bool
    count: int
    retry_after: int


def hit(key: str, *, limit: int, window_s: int, now: float | None = None) -> RateResult:
    now = time.time() if now is None else now
    bucket = int(now // window_s)
    name = f"{PREFIX}{key}:{window_s}:{bucket}"
    retry_after = max(1, int(window_s - (now % window_s)))
    try:
        cache.add(name, 0, timeout=window_s + 1)
        count = cache.incr(name)
    except Exception:  # noqa: BLE001 - an unavailable cache never blocks requests
        return RateResult(True, 0, 0)
    return RateResult(count <= limit, count, retry_after)


def peek(key: str, *, window_s: int, now: float | None = None) -> int:
    """How many events the current window already counted, without counting one."""
    now = time.time() if now is None else now
    try:
        return int(cache.get(f"{PREFIX}{key}:{window_s}:{int(now // window_s)}", 0))
    except Exception:  # noqa: BLE001 - an unavailable cache reads as zero: the limits fail open, by design
        return 0
