"""Cache prefix per instance and readiness without the cache (#675)."""

import json

import environ
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory

from skeleton.conf.base import cache_config


def config_for(environment):
    class Env(environ.Env):
        pass

    env = Env()
    env.ENVIRON = environment
    return cache_config(env)


def test_options_of_the_url_reach_django_in_upper_case():
    config = config_for({"CACHE_URL": "redis://:pw@redis:6379/2?key_prefix=inst-a&timeout=30"})
    assert config["KEY_PREFIX"] == "inst-a" and config["TIMEOUT"] == 30
    assert "key_prefix" not in config and "timeout" not in config


def test_the_instance_prefix_applies_and_two_prefixes_are_refused():
    config = config_for({"CACHE_URL": "redis://redis:6379/0", "SKELETON_CACHE_KEY_PREFIX": "inst-b"})
    assert config["KEY_PREFIX"] == "inst-b"
    with pytest.raises(ImproperlyConfigured, match="um lugar só"):
        config_for({"CACHE_URL": "redis://redis:6379/0?key_prefix=a", "SKELETON_CACHE_KEY_PREFIX": "b"})


def test_the_catalog_validates_the_prefix():
    from skeleton.instance.schema import CATALOG

    spec = CATALOG["cache_key_prefix"]
    assert spec.clean(" inst_a-1 ") == "inst_a-1"
    for bad in ("Inst", "com espaço", "x" * 51, "a.b"):
        with pytest.raises(ValueError):
            spec.clean(bad)


def test_two_prefixes_keep_the_rate_limit_keys_apart():
    from django.core.cache.backends.locmem import LocMemCache

    first = LocMemCache("shared", {"KEY_PREFIX": "inst-a"})
    second = LocMemCache("shared", {"KEY_PREFIX": "inst-b"})
    first.add("skeleton:rl:login", 0)
    first.incr("skeleton:rl:login")
    assert second.get("skeleton:rl:login") is None and first.get("skeleton:rl:login") == 1


def test_a_redis_without_prefix_warns_and_is_an_error_in_cluster(settings):
    from skeleton.system.checks.environment import cache_prefix_checks

    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": "redis://x"}}
    settings.SKELETON_INSTANCE_MODE = "standalone"
    assert [check.id for check in cache_prefix_checks()] == ["skeleton.W049"]
    settings.SKELETON_INSTANCE_MODE = "cluster"
    assert [check.id for check in cache_prefix_checks()] == ["skeleton.E049"]
    settings.CACHES["default"]["KEY_PREFIX"] = "inst-a"
    assert cache_prefix_checks() == []


class BrokenCache:
    def set(self, *args, **kwargs):
        raise ConnectionError("redis down")

    get = delete = set


class BrokenCursor:
    def __enter__(self):
        raise ConnectionError("database down")

    def __exit__(self, *args):
        return False


class Connection:
    def __init__(self, cursor):
        self.cursor = cursor


class WorkingCursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        pass

    def fetchone(self):
        return (1,)


def test_readiness_degrades_without_the_cache_and_fails_without_the_database(monkeypatch):
    from skeleton.api import health

    request = RequestFactory().get("/health/ready/")
    monkeypatch.setattr(health, "cache", BrokenCache())
    monkeypatch.setattr(health, "connection", Connection(WorkingCursor))
    response = health.probe_response(request)
    assert response.status_code == 200
    assert json.loads(response.content) == {"status": "ok", "degraded": ["cache"]}
    monkeypatch.setattr(health, "connection", Connection(BrokenCursor))
    response = health.probe_response(request)
    assert response.status_code == 503 and json.loads(response.content) == {"status": "unavailable"}
