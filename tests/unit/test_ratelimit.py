"""Fixed-window limiter (#48) on the local cache."""

from django.core.cache import cache

from skeleton.common.ratelimit import hit, peek


def test_window_counts_and_resets():
    cache.clear()
    base = 1_000_040.0  # 20 s into a 60 s window (1_000_020 is a window edge).
    results = [hit("unit", limit=3, window_s=60, now=base + i) for i in range(4)]
    assert [result.allowed for result in results] == [True, True, True, False]
    assert results[-1].retry_after == 60 - 23
    assert peek("unit", window_s=60, now=base) == 4
    assert hit("unit", limit=3, window_s=60, now=base + 40).allowed  # Next window.
    assert hit("other", limit=0, window_s=60, now=base).allowed is False


def test_cache_failure_lets_requests_through(monkeypatch):
    def broken(*args, **kwargs):
        raise ConnectionError("cache down")

    monkeypatch.setattr(cache, "add", broken)
    assert hit("down", limit=0, window_s=60).allowed
