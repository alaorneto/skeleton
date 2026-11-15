"""Classification of a request host and the origin of an organization address (#478)."""

import pytest

from skeleton.tenancy.site import HostKind, classify_host, organization_origin, platform_hosts

ORG = "organization"


@pytest.fixture
def sites(settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    settings.ALLOWED_HOSTS = [".example.test", "testserver", "portal.cliente.test"]
    return settings


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("app.example.test", HostKind("platform")),  # public URL
        ("APP.example.test:443", HostKind("platform")),  # case and port
        ("example.test", HostKind("platform")),  # bare base domain
        ("testserver", HostKind("platform")),  # explicit ALLOWED_HOSTS entry
        ("portal.cliente.test", HostKind("platform")),  # outside the base domain (#489 decides later)
        ("acme.example.test", HostKind(ORG, "acme")),
        ("Acme.Example.Test", HostKind(ORG, "acme")),
        ("acme.example.test.", HostKind(ORG, "acme")),  # trailing dot
        ("acme.example.test:8000", HostKind(ORG, "acme")),
        ("www.example.test", HostKind("unknown")),  # reserved
        ("a.b.example.test", HostKind("unknown")),  # two labels
        ("a_b.example.test", HostKind("unknown")),  # not a label
        ("xn--abc.example.test", HostKind("unknown")),
        ("xexample.test", HostKind("platform")),  # look-alike suffix, not under the base domain
    ],
)
def test_classify_host(sites, host, expected):
    assert classify_host(host) == expected


def test_without_base_domain_every_host_is_the_platform(settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    for host in ("acme.example.test", "www.example.test", "a.b.example.test", "anything"):
        assert classify_host(host) == HostKind("platform")


def test_health_host_is_a_platform_host(sites):
    sites.SKELETON_HEALTH_HOST = "health.example.test"
    assert "health.example.test" in platform_hosts()
    assert classify_host("health.example.test") == HostKind("platform")


def test_organization_origin(sites):
    assert organization_origin("acme") == "https://acme.example.test"
    sites.SKELETON_PUBLIC_URL = "http://app.example.test:8000"
    assert organization_origin("acme") == "http://acme.example.test:8000"
    assert organization_origin("Acme") is None
    assert organization_origin("www") is None
    sites.SKELETON_PUBLIC_URL = ""
    assert organization_origin("acme") is None
    sites.SKELETON_PUBLIC_URL = "https://app.example.test"
    sites.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    assert organization_origin("acme") is None


def test_shares_session(sites):
    from skeleton.tenancy.site import shares_session

    assert shares_session("example.test")
    assert shares_session("acme.example.test:8000")
    assert shares_session("APP.example.test")
    assert not shares_session("xexample.test")
    assert not shares_session("portal.cliente.test")
    assert not shares_session("")
    sites.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    assert not shares_session("acme.example.test")
