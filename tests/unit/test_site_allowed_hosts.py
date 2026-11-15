"""Allowed hosts with the verified custom domains (#489): static plus dynamic, never a wildcard."""

import uuid

import pytest
from django.db import OperationalError
from django.http.request import validate_host

from skeleton.tenancy import site_domains
from skeleton.tenancy.site import SiteAllowedHosts

ORGANIZATION = uuid.UUID("11111111-2222-4333-8444-555555555555")


@pytest.fixture(autouse=True)
def fresh_cache(settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.SKELETON_ORGANIZATION_CUSTOM_DOMAINS = True
    site_domains.reset_custom_hosts_cache()
    yield
    site_domains.reset_custom_hosts_cache()


def test_static_and_dynamic_hosts_validate(monkeypatch):
    monkeypatch.setattr(site_domains, "custom_hosts", lambda: {"portal.cliente.test": ORGANIZATION})
    hosts = SiteAllowedHosts([".example.test", "testserver"])
    assert validate_host("acme.example.test", hosts)
    assert validate_host("testserver", hosts)
    assert validate_host("portal.cliente.test", hosts)
    assert not validate_host("evil.test", hosts)
    assert not validate_host("x.portal.cliente.test", hosts)
    assert list.__iter__(hosts) is not None and list(list.__iter__(hosts)) == [".example.test", "testserver"]
    assert "*" not in hosts and hosts[0] == ".example.test"


def test_dynamic_entries_never_bring_a_wildcard(monkeypatch):
    monkeypatch.setattr(site_domains, "custom_hosts", lambda: {"*": ORGANIZATION, ".cliente.test": ORGANIZATION})
    hosts = SiteAllowedHosts(["testserver"])
    assert list(hosts) == ["testserver"]
    assert not validate_host("anything.test", hosts)


def test_cache_is_empty_when_the_feature_is_off_or_apps_are_not_ready(settings, monkeypatch):
    settings.SKELETON_ORGANIZATION_CUSTOM_DOMAINS = False
    assert site_domains.custom_hosts() == {}
    settings.SKELETON_ORGANIZATION_CUSTOM_DOMAINS = True
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    assert site_domains.custom_hosts() == {}
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    from django.apps import apps

    monkeypatch.setattr(apps, "ready", False)
    assert site_domains.custom_hosts() == {}


class _Rows:
    def __init__(self, result):
        self.result = result

    def filter(self, **kwargs):
        return self

    def values_list(self, *names):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _patch_rows(monkeypatch, result):
    from skeleton.tenancy import models

    monkeypatch.setattr(models.SiteDomainClaim, "objects", _Rows(result))


def test_database_error_keeps_the_last_good_value_then_fails_closed(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(site_domains.time, "monotonic", lambda: clock[0])
    _patch_rows(monkeypatch, [("portal.cliente.test", ORGANIZATION)])
    assert site_domains.custom_hosts() == {"portal.cliente.test": ORGANIZATION}
    _patch_rows(monkeypatch, OperationalError("database is down"))
    clock[0] += 31  # The cache expired: the database is asked and fails.
    assert site_domains.custom_hosts() == {"portal.cliente.test": ORGANIZATION}
    clock[0] += 300  # More than five minutes since the last good value.
    assert site_domains.custom_hosts() == {}


def test_cache_lasts_thirty_seconds(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(site_domains.time, "monotonic", lambda: clock[0])
    _patch_rows(monkeypatch, [("a.cliente.test", ORGANIZATION)])
    assert "a.cliente.test" in site_domains.custom_hosts()
    _patch_rows(monkeypatch, [])
    clock[0] += 10
    assert "a.cliente.test" in site_domains.custom_hosts()
    clock[0] += 25
    assert site_domains.custom_hosts() == {}
