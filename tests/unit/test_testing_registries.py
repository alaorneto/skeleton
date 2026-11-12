"""skeleton.testing.registries puts every original registry object back, also after an error (#336)."""

import pytest

from skeleton.testing.registries import (
    isolated_event_types,
    isolated_job_types,
    isolated_notification_types,
    isolated_token_scopes,
    table_spec_override,
)


class Boom(Exception):
    pass


def _noop(**kwargs):
    return None


@pytest.mark.parametrize("fail", [False, True])
def test_isolated_job_types_restores_registry_and_periodic(fail):
    from datetime import timedelta

    from skeleton.jobs import periodic, registry
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    before = (registry._registry, registry._duplicates, periodic._periodic)
    contents = (dict(before[0]), list(before[1]), dict(before[2]))
    with pytest.raises(Boom) if fail else _null():
        with isolated_job_types():
            assert registry._registry == {} and periodic._periodic == {}
            register_job_type(JobType("test.isolated", _noop, {}))
            register_periodic("test-isolated", "test.isolated", every=timedelta(minutes=5))
            if fail:
                raise Boom
    assert (registry._registry, registry._duplicates, periodic._periodic) == before
    assert all(
        now is then
        for now, then in zip((registry._registry, registry._duplicates, periodic._periodic), before, strict=True)
    )
    assert (dict(registry._registry), list(registry._duplicates), dict(periodic._periodic)) == contents
    with isolated_job_types(keep_existing=True):
        assert registry._registry == contents[0] and registry._registry is not before[0]


@pytest.mark.parametrize("fail", [False, True])
def test_isolated_event_types_keeps_core_and_restores(fail):
    from skeleton.events import registry

    before = (registry._types, registry._consumers, registry._duplicates)
    core = dict(registry._types)
    with pytest.raises(Boom) if fail else _null():
        with isolated_event_types():
            assert registry._types == core and registry._types is not before[0]
            assert registry._consumers == {}
            registry._types["test.isolated"] = object()
            if fail:
                raise Boom
    assert all(
        now is then
        for now, then in zip((registry._types, registry._consumers, registry._duplicates), before, strict=True)
    )
    assert "test.isolated" not in registry._types
    with isolated_event_types(keep_core=False):
        assert registry._types == {}
    consumers = dict(registry._consumers)
    with isolated_event_types(keep_consumers=True):
        assert registry._consumers == consumers and registry._consumers is not before[1]


@pytest.mark.parametrize("fail", [False, True])
def test_isolated_notification_types_restores(fail):
    from skeleton.notifications import registry

    before = (registry._types, registry._duplicates)
    contents = dict(before[0])
    with pytest.raises(Boom) if fail else _null():
        with isolated_notification_types():
            assert registry._types == {}
            registry._types["test.isolated"] = object()
            if fail:
                raise Boom
    assert registry._types is before[0] and registry._duplicates is before[1]
    assert registry._types == contents


@pytest.mark.parametrize("fail", [False, True])
def test_isolated_token_scopes_restores(fail):
    from skeleton.integrations.tokens import scopes

    before = scopes._named
    contents = dict(before)
    with pytest.raises(Boom) if fail else _null():
        with isolated_token_scopes():
            scopes.register_token_scope("test.isolated_feed", "Feed de teste", feed=True)
            assert "test.isolated_feed" in scopes._named
            if fail:
                raise Boom
    assert scopes._named is before and scopes._named == contents
    assert "test.isolated_feed" not in scopes._named


@pytest.mark.parametrize("fail", [False, True])
def test_table_spec_override_restores(fail):
    from skeleton.system import registry

    key = next(iter(sorted(registry._registry)))
    original = registry._registry[key]
    with pytest.raises(Boom) if fail else _null():
        with table_spec_override(key, description="overridden") as changed:
            assert registry._registry[key] is changed and changed.description == "overridden"
            if fail:
                raise Boom
    assert registry._registry[key] is original


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
