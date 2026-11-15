"""System checks of the organization sites (#478, #481)."""

from skeleton.tenancy.checks import organization_session_checks, organization_site_checks


def ids(results):
    return [result.id for result in results]


def test_base_domain_check(settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    settings.SKELETON_PUBLIC_URL = ""
    assert organization_site_checks() == []
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    assert "skeleton.E038" not in ids(organization_site_checks())
    settings.SKELETON_PUBLIC_URL = ""
    assert ids(organization_site_checks()).count("skeleton.E038") == 1
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "https://example.test"
    assert ids(organization_site_checks()).count("skeleton.E038") == 1


def test_session_checks(settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    settings.SESSION_COOKIE_DOMAIN = None
    settings.CSRF_COOKIE_DOMAIN = None
    assert not {"skeleton.E039", "skeleton.W039"} & set(ids(organization_session_checks()))
    settings.SESSION_COOKIE_DOMAIN = ".example.test"
    assert ids(organization_session_checks()) == ["skeleton.E039"]
    settings.SESSION_COOKIE_DOMAIN = None
    settings.CSRF_COOKIE_DOMAIN = ".example.test"
    assert ids(organization_session_checks()) == ["skeleton.E039"]
    settings.CSRF_COOKIE_DOMAIN = None
    settings.SKELETON_PUBLIC_URL = "https://app.other.test"
    assert ids(organization_session_checks()) == ["skeleton.W039"]
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    settings.SESSION_COOKIE_DOMAIN = ".example.test"
    assert organization_session_checks() == []
