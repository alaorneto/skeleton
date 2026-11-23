"""The platform update notice (#501, #502): cached, never raising, informative without agent."""

import time
from dataclasses import replace

import pytest

from skeleton.deployment import DeploymentInventory
from skeleton.system import releases
from skeleton.system.releases import NOTICE_CACHE_KEY, invalidate_update_notice, update_notice
from skeleton.versioning import core_version

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
    settings.SKELETON_DEPLOYMENT_STATE = DeploymentInventory(0, {}, (), (), ())
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
    assert double.calls == [2] and cache.get(f"{NOTICE_CACHE_KEY}:0") == {"mode": None}


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


RUNNING = core_version()["version"]


def core_path(installed, latest="9.0.0"):
    return {"component_id": "django-skeleton", "installed": installed, "latest": latest, "steps": [{"version": latest}]}


def test_a_new_generation_never_reads_the_previous_notice(cache, monkeypatch, settings):
    """#739: the reconciliation that invalidates may not share this cache; the generation does."""
    double = agent(monkeypatch, {"paths": [core_path(RUNNING)]})
    assert update_notice()["core"] == "9.0.0"
    cache.set(f"{NOTICE_CACHE_KEY}:0", {"mode": "agent", "core": RUNNING, "apps": 0}, 300)  # Stale, never deleted.
    settings.SKELETON_DEPLOYMENT_STATE = replace(settings.SKELETON_DEPLOYMENT_STATE, generation=1)
    double.answer = {"paths": [core_path(RUNNING, latest=None) | {"steps": []}]}
    assert update_notice() is None and len(double.calls) == 2
    invalidate_update_notice()  # Deletes the running generation's key only.
    assert cache.get(f"{NOTICE_CACHE_KEY}:0") is not None and cache.get(f"{NOTICE_CACHE_KEY}:1") is None


def test_an_answer_for_another_core_is_no_notice_for_a_short_while(cache, monkeypatch):
    """#739: the agent still evaluates the previous release; its path is not this process's."""
    double = agent(monkeypatch, {"paths": [core_path("0.0.1", latest=RUNNING)]})
    assert update_notice() is None
    assert update_notice() is None and len(double.calls) == 1
    assert cache.get(f"{NOTICE_CACHE_KEY}:0") == {"mode": None}
    expires = cache._expire_info[cache.make_and_validate_key(f"{NOTICE_CACHE_KEY}:0")]  # LocMem internals.
    assert expires <= time.time() + releases.NOTICE_FAILURE_TTL


def test_an_answer_for_another_app_version_is_no_notice(cache, monkeypatch, settings):
    manifest = {"app_id": "org.example.notes", "version": "1.4.0"}
    settings.SKELETON_DEPLOYMENT_STATE = replace(
        settings.SKELETON_DEPLOYMENT_STATE, apps=({"state": "installed", "manifest": manifest},)
    )
    app = {"component_id": "org.example.notes", "latest": "1.4.0", "steps": [{"version": "1.4.0"}]}
    agent(monkeypatch, {"paths": [core_path(RUNNING, latest=None) | {"steps": []}, app | {"installed": "1.3.0"}]})
    assert update_notice() is None
    cache.clear()
    agent(monkeypatch, {"paths": [app | {"installed": "1.4.0", "latest": "1.5.0"}]})
    assert update_notice() == {"mode": "agent", "core": None, "apps": 1, "checked_at": None}
