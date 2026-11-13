"""Attendees (#72): same organization only, reading by invitation, RSVP for oneself, notifications."""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from skeleton.contrib.calendar import attendees, services
from skeleton.contrib.calendar.policies import readable_events
from tools.security_fixtures import actor_tenant

START = datetime(2026, 12, 2, 15, 0, tzinfo=UTC)


@pytest.fixture
def meeting(agenda):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = services.ensure_personal_calendar(agenda.alice)
        event = services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={"title": "Planejamento", "starts_at": START, "ends_at": START + timedelta(hours=1)},
        )
    return event


def notifications_of(agenda, user, organization):
    from skeleton.notifications.models import Notification

    with actor_tenant(agenda.admin, organization):
        return list(Notification.objects.values_list("type_code", flat=True))


def test_only_people_of_the_organization_can_be_invited(agenda, meeting):
    from skeleton.accounts.services import create_service_account

    with actor_tenant(agenda.admin, agenda.a):
        service = create_service_account(actor=agenda.admin, name="Robô")
    with actor_tenant(agenda.alice, agenda.a):
        for user in (agenda.outsider, service):
            with pytest.raises(ValidationError):
                attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(user.pk,))
        [invitation] = attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,))
        assert invitation.response == "needs_action"
        assert attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,)) == []
    assert "calendar.invited" in notifications_of(agenda, agenda.bob, agenda.a)


def test_attendee_reads_but_does_not_edit_and_removal_revokes(agenda, meeting):
    with actor_tenant(agenda.alice, agenda.a):
        [invitation] = attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,))
    with actor_tenant(agenda.bob, agenda.a):
        assert readable_events(agenda.bob).filter(pk=meeting.pk).exists()  # A personal calendar of Alice.
        from skeleton.contrib.calendar.service_api import event_info

        assert event_info(agenda.bob, readable_events(agenda.bob).get(pk=meeting.pk)).title == "Planejamento"
        with pytest.raises(PermissionDenied):
            services.update_event(actor=agenda.bob, event_id=meeting.pk, data={"title": "x"}, expected_revision=1)
    with actor_tenant(agenda.alice, agenda.a):
        attendees.remove(actor=agenda.alice, attendee_id=invitation.pk, expected_revision=invitation.revision)
    with actor_tenant(agenda.bob, agenda.a):
        assert not readable_events(agenda.bob).filter(pk=meeting.pk).exists()


def test_rsvp_only_for_oneself(agenda, meeting):
    with actor_tenant(agenda.alice, agenda.a):
        [invitation] = attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,))
    revision = invitation.revision
    with actor_tenant(agenda.bob, agenda.a):
        answered = attendees.respond(
            actor=agenda.bob, event_id=meeting.pk, response="accepted", expected_revision=revision
        )
        assert answered.response == "accepted"
        with pytest.raises(ValidationError):
            attendees.respond(
                actor=agenda.bob, event_id=meeting.pk, response="maybe", expected_revision=answered.revision
            )
    with actor_tenant(agenda.admin, agenda.a):  # Not invited: someone else's invitation does not exist for them.
        with pytest.raises(PermissionDenied):
            attendees.respond(
                actor=agenda.admin, event_id=meeting.pk, response="declined", expected_revision=answered.revision
            )


def test_updates_and_cancellation_notify_attendees(agenda, meeting):
    with actor_tenant(agenda.alice, agenda.a):
        attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,))
        updated = services.update_event(
            actor=agenda.alice, event_id=meeting.pk, data={"title": "Novo horário"}, expected_revision=meeting.revision
        )
        services.cancel_event(actor=agenda.alice, event_id=meeting.pk, expected_revision=updated.revision)
    types = notifications_of(agenda, agenda.bob, agenda.a)
    assert {"calendar.invited", "calendar.updated", "calendar.cancelled"} <= set(types)


def test_shared_calendar_readers_and_inactive_attendees(agenda, meeting):
    from skeleton.access.services import update_membership

    with actor_tenant(agenda.alice, agenda.a):
        attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,))
    with actor_tenant(agenda.admin, agenda.a):
        update_membership(agenda.bob_membership, user=agenda.admin, data={"is_active": False})
    from skeleton.contrib.calendar.models import EventAttendee

    with actor_tenant(agenda.alice, agenda.a):
        assert attendees.attendee_users(meeting) == ()  # Inactive members are no longer notified.
        assert EventAttendee.objects.filter(event=meeting).count() == 1  # The row stays (history).


def _invited(organization):
    from skeleton.notifications.models import Notification
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:invited-notifications", organization_id=organization.pk):
        return list(Notification.objects.filter(type_code="calendar.invited").values_list("recipients", flat=True))


def test_second_invitation_batch_is_notified(agenda, meeting):
    """Two batches of invitations to the same event notify each batch (#235)."""
    with actor_tenant(agenda.alice, agenda.a):
        attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,))
        attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.admin.pk,))
    recipients = sorted(row["users"] for row in _invited(agenda.a))
    assert recipients == sorted([[str(agenda.bob.pk)], [str(agenda.admin.pk)]])
    assert _invited(agenda.b) == []


def test_same_invitation_twice_does_not_duplicate_the_notification(agenda, meeting):
    with actor_tenant(agenda.alice, agenda.a):
        attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,))
        assert attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,)) == []
    assert len(_invited(agenda.a)) == 1


def test_deleting_the_event_notifies_cancellation(agenda, meeting):
    with actor_tenant(agenda.alice, agenda.a):
        attendees.invite(actor=agenda.alice, event_id=meeting.pk, user_ids=(agenda.bob.pk,))
        meeting.refresh_from_db()
        services.delete_event(actor=agenda.alice, event_id=meeting.pk, expected_revision=meeting.revision)
    assert "calendar.cancelled" in notifications_of(agenda, agenda.bob, agenda.a)
