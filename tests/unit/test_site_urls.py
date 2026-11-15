"""Absolute ``next`` URLs accepted only for an enabled organization address (#486)."""

import pytest

from skeleton.tenancy import site as site_module

ENABLED = {"acme"}


@pytest.fixture(autouse=True)
def sites(settings, monkeypatch):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    settings.ALLOWED_HOSTS = [".example.test", "testserver"]
    monkeypatch.setattr(site_module, "find_site", lambda label: object() if label in ENABLED else None)


@pytest.mark.parametrize(
    "url",
    ["https://acme.example.test/", "https://acme.example.test/tables/x/?q=1", "https://ACME.example.test/"],
)
def test_accepted(url):
    assert site_module.is_site_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/",
        "https://acme.example.test.evil.test/",
        "https://acme.example.test@evil.test/",
        "//evil.test/",
        "//acme.example.test/",
        "https://naoexiste.example.test/",
        "https://desligada.example.test/",
        "http://acme.example.test/",
        "https://www.example.test/",
        "https://acme.example.test:8443/",
        "https://app.example.test/",
        "https://a.b.example.test/",
        "https://acme.example.test\\@evil.test/",
        "https://acme.example.test/\nLocation: https://evil.test/",
        "/tables/x/",
        "",
        None,
    ],
)
def test_refused(url):
    assert not site_module.is_site_url(url)


def test_port_must_match_the_public_url(settings):
    settings.SKELETON_PUBLIC_URL = "http://app.example.test:8000"
    assert site_module.is_site_url("http://acme.example.test:8000/")
    assert not site_module.is_site_url("http://acme.example.test/")
