"""Jobs, notifications, events and token scopes of the calendar, registered at startup."""


def register_calendar_extensions() -> None:
    from .attendees import register_attendee_notifications

    register_attendee_notifications()
