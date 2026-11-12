"""Emails carry the brand of their organization (#173): layout, colour, inline logo, language."""

import io
from datetime import timedelta

import pytest
from django.core import mail
from django.test import override_settings

from skeleton.notifications.channels import delivery as delivery_module
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.service_api.organizations import OrganizationService
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import acting_as, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
TWO_LANGUAGES = [("pt-br", "Português"), ("en", "English")]


@pytest.fixture
def scenario(monkeypatch, settings):

    from examples.notes.models import Note

    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    with isolated_notification_types():
        monkeypatch.setattr(delivery_module, "RETRY_SCHEDULE", (timedelta(0),) * 4)
        register_notification_type(
            NotificationType(
                "notes.mention",
                "Você foi mencionado",
                snapshot_fields=("title",),
                target_table_key="example_notes.note",
                default_channels=("in_app", "email"),
            )
        )
        yield build_service_scenario(Note, slug="email-brand")


def brand(scenario, organization, admin, *, color="", name="", language="", logo=False):
    from skeleton.tenancy.branding import replace_logo

    service = OrganizationService()
    with acting_as(admin, organization) as actor:
        revision = service.settings(actor=actor).revision
        service.update_settings(
            actor=actor,
            display_name=name,
            default_language=language,
            default_timezone="",
            brand_color=color,
            expected_revision=revision,
        )
        if logo:
            upload = io.BytesIO(PNG)
            upload.name, upload.size = "logo.png", len(PNG)
            replace_logo(actor=actor, upload=upload, expected_revision=revision + 1)


def mention_email(scenario, organization, admin, user):
    note = scenario.create(organization, owner=admin, code="B-1", title="Plano de voo")
    with scenario.acting(admin, organization):
        notify("notes.mention", recipients=Recipients(users=(user.pk,)), target=note)
    run_pending_jobs()
    return next(message for message in mail.outbox if user.email in message.to)


def html_of(message):
    return next(content for content, mimetype in message.alternatives if mimetype == "text/html")


def inline_parts(message):
    parts = []
    for attachment in message.attachments:
        if hasattr(attachment, "get"):  # email.message.MIMEPart
            parts.append((attachment.get("Content-ID"), attachment.get_content_type()))
    return parts


def test_notification_email_uses_the_layout_and_the_default_colour(scenario):
    message = mention_email(scenario, scenario.a, scenario.admin_a, scenario.member)
    html = html_of(message)
    assert "background:#1d4ed8" in html  # Default brand colour of the layout.
    assert "Service contract A" in html  # Organization name in the header.
    assert "cid:" not in html and inline_parts(message) == []


def test_brand_colour_name_and_inline_logo(scenario):
    brand(scenario, scenario.a, scenario.admin_a, color="#7c2d12", name="Marca A", logo=True)
    message = mention_email(scenario, scenario.a, scenario.admin_a, scenario.member)
    html = html_of(message)
    assert "background:#7c2d12" in html
    assert 'src="cid:organization-logo"' in html and 'alt="Marca A"' in html
    assert inline_parts(message) == [("<organization-logo>", "image/png")]


def test_one_organization_never_brands_the_email_of_another(scenario):
    brand(scenario, scenario.a, scenario.admin_a, color="#7c2d12", name="Marca A", logo=True)
    message = mention_email(scenario, scenario.b, scenario.admin_b, scenario.member)
    html = html_of(message)
    assert "Marca A" not in html and "#7c2d12" not in html
    assert inline_parts(message) == []


def test_invitation_email_is_templated_branded_and_follows_the_organization_language(scenario):
    from skeleton.access.models import Role
    from skeleton.tenancy.invitations import issue_invitation

    with override_settings(LANGUAGES=TWO_LANGUAGES):
        brand(scenario, scenario.a, scenario.admin_a, color="#065f46", name="Marca A", language="en", logo=True)
        with system_context(reason="test:email-brand-role", organization_id=scenario.a.pk):
            role = Role.objects.get(code="member")
        with acting_as(scenario.admin_a, scenario.a):
            issue_invitation(user=scenario.admin_a, email="invited-brand@example.test", role=role)
    message = next(message for message in mail.outbox if "invited-brand@example.test" in message.to)
    assert "Marca A" in message.subject
    assert "https://app.example.test/invite/" in message.body
    html = html_of(message)
    assert '<html lang="en">' in html  # Organization default language, not the inviter's profile.
    assert "background:#065f46" in html and "/invite/" in html
    assert inline_parts(message) == [("<organization-logo>", "image/png")]
