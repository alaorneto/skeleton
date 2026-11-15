"""System checks of the organization sites (#478, #481)."""

from skeleton.tenancy.checks import organization_site_checks


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
