"""Calendar suite (#84): collected only with SKELETON_BUNDLED_APPS=calendar (CI job "calendar")."""

import pytest
from django.apps import apps

if not apps.is_installed("skeleton.contrib.calendar"):
    collect_ignore_glob = ["test_*.py"]

PASSWORD = "Fixture-only-password-2026!"


@pytest.fixture
def agenda(skeleton_db):
    """Security fixture with the calendar available and active in A and B, member access in A."""
    from skeleton.contrib.calendar.services import grant_member_access
    from skeleton.testing.applications import activate_app, open_app
    from tools.security_fixtures import actor_tenant, build_security_fixture

    fixture = build_security_fixture()
    open_app("skeleton_calendar")
    for organization in (fixture.a, fixture.b):
        activate_app("skeleton_calendar", organization.pk)
    with actor_tenant(fixture.admin, fixture.a):
        grant_member_access(actor=fixture.admin)
    return fixture


@pytest.fixture
def login():
    from django.test import Client
    from django.urls import reverse

    def sign_in(user):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("account_login"))
        response = client.post(
            reverse("account_login"),
            {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
        )
        assert response.status_code == 302
        return client

    return sign_in
