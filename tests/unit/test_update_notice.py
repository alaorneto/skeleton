"""The platform update notice (#501, #502): cached, never raising, informative without agent."""

import pytest

from skeleton.system import releases
from skeleton.system.releases import NOTICE_CACHE_KEY, invalidate_update_notice, update_notice

STATUS = {
    "checked_at": "2026-11-15T12:00:00+00:00",
    "paths": [
        {"component_id": "django-skeleton", "latest": "0.7.1", "steps": [{"version": "0.6.4", "source_id": "s"}]},
        {"component_id": "org.example.notes", "latest": "1.4.0", "steps": [{"version": "1.4.0", "source_id": "s"}]},
        {"component_id": "org.example.calc", "latest": "3.0.0", "steps": []},
    ],
}


class Agent:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def update_status(self, *, timeout):
        self.calls.append(timeout)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def cache(settings):
    from django.core.cache import cache

    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "notice"}}
    settings.SKELETON_AGENT_SOCKET = "/run/skeleton/agent.sock"
    cache.clear()
    yield cache
    cache.clear()


def agent(monkeypatch, answer):
    double = Agent(answer)
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    return double


def test_notice_summarizes_steps_and_is_cached(cache, monkeypatch):
    double = agent(monkeypatch, STATUS)
    expected = {"mode": "agent", "core": "0.7.1", "apps": 1, "checked_at": "2026-11-15T12:00:00+00:00"}
    assert update_notice() == expected
    assert update_notice() == expected
    assert double.calls == [2]
    invalidate_update_notice()
    assert update_notice() == expected and double.calls == [2, 2]


def test_no_steps_means_no_notice_and_is_cached_too(cache, monkeypatch):
    double = agent(monkeypatch, {"paths": [{"component_id": "django-skeleton", "latest": None, "steps": []}]})
    assert update_notice() is None and update_notice() is None
    assert double.calls == [2] and cache.get(NOTICE_CACHE_KEY) == {"mode": None}


@pytest.mark.parametrize(
    "answer",
    [
        releases.DeploymentUnavailable("down"),
        TimeoutError(),
        ValueError("agent_unavailable"),
        {"paths": "nope"},
        [],
    ],
)
def test_failures_are_no_notice_for_a_short_while(cache, monkeypatch, answer):
    double = agent(monkeypatch, answer)
    assert update_notice() is None
    assert update_notice() is None and len(double.calls) == 1


def test_cache_outage_never_raises(cache, monkeypatch):
    agent(monkeypatch, STATUS)

    class Broken:
        def get(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr("django.core.cache.cache", Broken())
    assert update_notice() is None
