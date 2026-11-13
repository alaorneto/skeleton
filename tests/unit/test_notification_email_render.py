"""Rendering of notification emails by language and time zone, without a database (#54)."""

from datetime import UTC, datetime
from types import SimpleNamespace

from skeleton.notifications.channels.email import build_message


def person(language, zone):
    return SimpleNamespace(
        email="person@example.test", profile=SimpleNamespace(language=language, timezone=zone, display_name="")
    )


CONTEXT = {
    "label": "Você foi mencionado",
    "type_code": "notes.mention",
    "snapshot": {"title": "Plano"},
    "payload": {},
    "created_at": datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
    "open_url": "https://app.example.test/o/x/notifications/y/open/",
}


def test_english_profile_gets_english_text_in_its_time_zone(settings):
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    settings.LANGUAGES = [("pt-br", "Português"), ("en", "English")]  # The core ships pt-BR only (#170).
    message = build_message(person("en", "America/Sao_Paulo"), type_code="notes.mention", context=CONTEXT)
    assert message.subject == "Você foi mencionado"
    assert "Open: https://app.example.test/o/x/notifications/y/open/" in message.body
    assert "Received on 2026-11-10 09:00." in message.body  # 12:00 UTC is 09:00 in São Paulo.
    assert message.extra_headers["List-Unsubscribe"] == "<https://app.example.test/profile/notifications/>"
    assert message.alternatives[0].mimetype == "text/html"


def test_portuguese_is_the_default_and_utc_stays_utc():
    message = build_message(person("pt-br", "UTC"), type_code="notes.mention", context=CONTEXT)
    assert "Abrir: " in message.body and "Recebido em 10/11/2026 12:00." in message.body
    assert "Plano" in message.body


def test_list_unsubscribe_is_omitted_without_public_url(settings):
    """A relative unsubscribe URL is useless to a mail client: no header without the origin (#279)."""
    settings.SKELETON_PUBLIC_URL = ""
    message = build_message(person("pt-br", "UTC"), type_code="notes.mention", context=CONTEXT)
    assert "List-Unsubscribe" not in message.extra_headers
    assert message.extra_headers["Auto-Submitted"] == "auto-generated"


def test_list_unsubscribe_is_absolute_with_public_url(settings):
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    message = build_message(person("pt-br", "UTC"), type_code="notes.mention", context=CONTEXT)
    assert message.extra_headers["List-Unsubscribe"] == "<https://app.example.test/profile/notifications/>"
