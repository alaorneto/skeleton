"""The registry and the combination rule of organization limits (#695), without a database."""

from uuid import uuid4

import pytest

from skeleton.common import limits


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(limits, "_metrics", {})
    monkeypatch.setattr(limits, "_sources", {})
    monkeypatch.setattr(limits, "_resolver", None)
    return limits


def test_combine_keeps_unlimited_without_a_default_or_contributions():
    assert limits.combine(None, []) is None
    assert limits.combine(None, [3]) == 3  # A plan without an instance default: the plan's sum, as before.
    assert limits.combine(1, []) == 1
    assert limits.combine(1, [2, 7]) == 10  # The default plus app X (+2) and app Y (+7).


def test_metric_codes_and_duplicates(registry):
    counter = lambda organization_id: 0  # noqa: E731
    registry.register_limit_metric("members", counter)
    registry.register_limit_metric("members", counter)  # Same counter: no-op.
    registry.register_limit_metric("acme_crm.contacts", counter, label="Contatos")
    for code in ("Members", "acme.", "a.b.c", "", None, "1x"):
        with pytest.raises(ValueError, match="Invalid limit metric"):
            registry.register_limit_metric(code, counter)
    with pytest.raises(ValueError, match="already registered"):
        registry.register_limit_metric("members", lambda organization_id: 1)
    assert registry.registered_limit_metrics() == {"members", "acme_crm.contacts"}
    assert registry.limit_metric("acme_crm.contacts").label == "Contatos"
    with pytest.raises(KeyError):
        registry.limit_metric("unknown")


def test_sources_run_by_order_and_refuse_a_different_one_under_the_same_name(registry):
    def first(organization_id, metric):
        return ()

    def second(organization_id, metric):
        return ()

    registry.register_limit_source("platform", second, order=300)
    registry.register_limit_source("apps", first, order=100)
    registry.register_limit_source("apps", first, order=100)  # Same: no-op.
    assert [name for name, _ in registry.limit_sources()] == ["apps", "platform"]
    with pytest.raises(ValueError, match="already registered"):
        registry.register_limit_source("apps", second, order=100)


def test_check_without_a_resolver_limits_nothing_and_the_resolver_is_single(registry):
    calls = []
    registry.check(uuid4(), "members", 1)  # No resolver: nothing happens.

    def resolver(organization_id, metric, increment):
        calls.append((metric, increment))

    registry.register_limit_resolver(resolver)
    registry.register_limit_resolver(resolver)  # Same: no-op.
    with pytest.raises(ValueError, match="already registered"):
        registry.register_limit_resolver(lambda organization_id, metric, increment: None)
    registry.check(uuid4(), "members", 2)
    assert calls == [("members", 2)]


def test_the_entitlement_hook_delegates_to_the_core_limits(registry):
    from skeleton.common.entitlements import check_limit

    seen = []
    registry.register_limit_resolver(lambda organization_id, metric, increment: seen.append(metric))
    check_limit(uuid4(), "external_people", 1)
    assert seen == ["external_people"]
