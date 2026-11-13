"""Invitations issued without a request use SKELETON_PUBLIC_URL (#169)."""

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import RequestFactory, override_settings

from skeleton.accounts.services import bootstrap_identity
from skeleton.tenancy.context import tenant_context
from skeleton.tenancy.invitations import issue_invitation

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Invitation-public-url-2026!731"


@pytest.fixture
def admin():
    return bootstrap_identity(
        email="invite-url@example.test", organization="invite-url", name="Invitation URL", password=PASSWORD
    )


def invite(admin, email, request=None):
    from skeleton.access.models import Role

    user, organization = admin
    with tenant_context(organization_id=organization.pk, actor=user, source="test:invite-url"):
        return issue_invitation(user=user, email=email, role=Role.objects.get(code="member"), request=request)


def body_for(email):
    return next(message.body for message in mail.outbox if email in message.to)


@override_settings(SKELETON_PUBLIC_URL="https://app.example.test/")
def test_without_request_the_link_uses_the_public_url(admin):
    invite(admin, "no-request@example.test")
    assert "https://app.example.test/invite/" in body_for("no-request@example.test")


@override_settings(SKELETON_PUBLIC_URL="")
def test_without_request_and_public_url_the_error_names_the_setting(admin):
    with pytest.raises(ValidationError) as error:
        invite(admin, "missing-url@example.test")
    assert "SKELETON_PUBLIC_URL" in str(error.value)


@override_settings(SKELETON_PUBLIC_URL="https://app.example.test")
def test_with_request_the_request_host_still_wins(admin):
    invite(admin, "with-request@example.test", RequestFactory().get("/", HTTP_HOST="testserver"))
    assert "http://testserver/invite/" in body_for("with-request@example.test")
