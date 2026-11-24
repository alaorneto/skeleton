"""The text of a notification is rendered at fan-out, per person, and stored on the row (#783)."""

import pytest
from django.core import mail
from django.urls import reverse
from django.utils.translation import gettext_lazy

from skeleton.notifications.models import NotificationRecipient
from skeleton.notifications.registry import NotificationField, NotificationType, register_notification_type
from skeleton.notifications.retention import forget_person
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]

FIELDS = (
    NotificationField("title", "Título"),
    NotificationField("count", "Quantidade", kind="number"),
    NotificationField("total", "Valor", kind="money", currency_field="currency"),
    NotificationField("due", "Vencimento", kind="date"),
    NotificationField("who", "Pessoa", kind="person"),
)


@pytest.fixture
def scenario(settings):
    from examples.notes.models import Note

    settings.LANGUAGES = [("pt-br", "Português"), ("en", "English")]
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    with isolated_notification_types():
        register_notification_type(
            NotificationType(
                "notes.event",
                gettext_lazy("Evento"),
                payload_fields=("title", "count", "total", "currency", "due", "who"),
                fields=FIELDS,
                title="Convite para {title}",
                body="{count} itens · {total} · {due} · {who}",
                group_key="event:{title}",
                default_channels=("in_app", "email"),
                owner_app="example_notes",
            )
        )
        register_notification_type(NotificationType("notes.plain", "Aviso simples", owner_app="example_notes"))
        register_notification_type(
            NotificationType(
                "notes.linked",
                gettext_lazy("Com link"),
                payload_fields=("title",),
                fields=(NotificationField("title", "Título"),),
                title="Novo: {title}",
                body="Abra em {link}",
                default_channels=("in_app", "email"),
                open_url=lambda user, row: f"/item/{row.payload['title']}/",
                owner_app="example_notes",
            )
        )
        register_notification_type(
            NotificationType(
                "notes.personal",
                gettext_lazy("Pessoal"),
                payload_fields=("due",),
                fields=(NotificationField("due", "Vencimento", kind="date"),),
                title="Olá, {destinatario_primeiro_nome}: vence em {due}",
                body="{destinatario} · {plataforma} · {organizacao} · {aviso} · {data_atual}",
                owner_app="example_notes",
            )
        )
        yield build_service_scenario(Note, slug="texts")


def set_profile(user, *, language="", zone="", name=""):
    from skeleton.accounts.models import UserProfile

    with system_context(reason="test:texts-profile"):
        profile = UserProfile.objects.get(user=user)
        profile.language, profile.timezone, profile.display_name = language, zone, name
        profile.save(update_fields={"language", "timezone", "display_name"})


def send(scenario, kind="notes.event", *, users, **payload):
    with scenario.acting(scenario.admin_a, scenario.a):
        notify(kind, recipients=Recipients(users=tuple(user.pk for user in users)), payload=payload)
    run_pending_jobs()


def row_of(scenario, user):
    with system_context(reason="test:texts-row", organization_id=scenario.a.pk):
        return NotificationRecipient.objects.get(user=user)


PAYLOAD = {"title": "Reunião", "count": 12345, "total": 123456, "currency": "BRL", "due": "2026-11-15"}


def test_each_person_gets_the_text_in_their_language_with_formatted_values(scenario):
    set_profile(scenario.member, language="en", name="Membro")
    set_profile(scenario.denied, language="pt-br", name="Negado")
    set_profile(scenario.admin_a, name="Ana Admin")
    send(scenario, users=(scenario.member, scenario.denied), who=str(scenario.admin_a.pk), **PAYLOAD)
    english, portuguese = row_of(scenario, scenario.member), row_of(scenario, scenario.denied)
    assert english.title == portuguese.title == "Convite para Reunião"
    assert portuguese.body == "12.345 itens · 1.234,56 BRL · 15/11/2026 · Ana Admin"
    assert english.body.startswith("12,345 itens · 1,234.56 BRL")
    assert english.subject_people == [str(scenario.admin_a.pk)]


def test_a_person_outside_the_organization_is_never_named(scenario):
    set_profile(scenario.admin_b, name="Bruno de B")
    send(scenario, users=(scenario.member,), who=str(scenario.admin_b.pk), **PAYLOAD)
    assert row_of(scenario, scenario.member).body.endswith("Pessoa indisponível")


def test_grouping_rewrites_the_text_of_the_unread_row(scenario):
    send(scenario, users=(scenario.member,), **PAYLOAD)
    send(scenario, users=(scenario.member,), **{**PAYLOAD, "count": 2})
    row = row_of(scenario, scenario.member)
    assert row.count == 2 and row.body.startswith("2 itens")


def test_a_type_without_text_keeps_the_label(scenario):
    send(scenario, "notes.plain", users=(scenario.member,))
    row = row_of(scenario, scenario.member)
    assert (row.title, row.body) == ("Aviso simples", "")


def test_values_are_escaped_in_the_inbox_and_in_the_email(scenario):
    mail.outbox.clear()
    send(scenario, users=(scenario.member,), **{**PAYLOAD, "title": "<script>x</script>"})
    client = support.login(scenario.member, password=FIXTURE_PASSWORD)
    page = client.get(reverse("notifications:organization", kwargs={"organization_id": scenario.a.pk}))
    html = page.content.decode()
    assert "Convite para &lt;script&gt;x&lt;/script&gt;" in html and "<script>x</script>" not in html
    message = next(item for item in mail.outbox if scenario.member.email in item.to)
    assert message.subject == "Convite para <script>x</script>"  # Plain text, never HTML.
    body_html = message.alternatives[0][0]
    assert "&lt;script&gt;" in body_html and "<script>x</script>" not in body_html


def test_the_email_of_someone_without_an_inbox_row_is_rendered_in_the_job(scenario):
    from skeleton.notifications.preferences import set_preference

    with scenario.acting(scenario.member, scenario.a):
        set_preference(scenario.member, type_code="notes.event", channel="in_app", mode="off", expected_revision=None)
    mail.outbox.clear()
    send(scenario, users=(scenario.member,), **PAYLOAD)
    with system_context(reason="test:texts-none", organization_id=scenario.a.pk):
        assert not NotificationRecipient.objects.filter(user=scenario.member).exists()
    message = next(item for item in mail.outbox if scenario.member.email in item.to)
    assert message.subject == "Convite para Reunião"


def test_forgetting_a_person_redacts_the_rows_that_name_them(scenario):
    set_profile(scenario.admin_a, name="Ana Admin")
    send(scenario, users=(scenario.member,), who=str(scenario.admin_a.pk), **PAYLOAD)
    with system_context(reason="test:texts-forget", organization_id=scenario.a.pk):
        result = forget_person(scenario.admin_a.pk)
        row = NotificationRecipient.objects.get(user=scenario.member)
    assert result["redacted"] == 1
    assert (row.title, row.body, row.subject_people) == ("Evento", "", [])


def test_a_text_that_names_the_recipient_is_rendered_for_each_person(scenario, monkeypatch):
    from django.utils import timezone

    from skeleton.notifications import rendering

    calls = []
    original = rendering.render_notification
    monkeypatch.setattr(rendering, "render_notification", lambda *a, **k: calls.append(1) or original(*a, **k))
    set_profile(scenario.member, language="pt-br", zone="America/Sao_Paulo", name="Maria da Silva")
    set_profile(scenario.denied, language="pt-br", zone="America/Sao_Paulo", name="")
    with system_context(reason="test:texts-names"):
        scenario.denied.first_name, scenario.denied.last_name = "João", "Souza"
        scenario.denied.save(update_fields={"first_name", "last_name"})
    send(scenario, "notes.personal", users=(scenario.member, scenario.denied), due="2026-11-30")
    maria, joao = row_of(scenario, scenario.member), row_of(scenario, scenario.denied)
    assert maria.title == "Olá, Maria: vence em 30/11/2026"
    assert joao.title == "Olá, João: vence em 30/11/2026"
    with system_context(reason="test:texts-sent", organization_id=scenario.a.pk):
        sent = maria.notification.created_at
    with timezone.override("America/Sao_Paulo"):
        today = timezone.localtime(sent).strftime("%d/%m/%Y")
    assert maria.body.startswith("Maria da Silva · Skeleton · ")
    assert maria.body.endswith(f" · Pessoal · {today}")
    assert joao.body.startswith("João Souza · ")
    assert len(calls) == 2  # Same language and time zone, but one rendering per person.
    assert maria.subject_people == []  # The recipient is not a subject: forgetting them deletes their rows.


def test_a_text_without_the_recipient_is_rendered_once_per_language_and_zone(scenario, monkeypatch):
    from skeleton.notifications import rendering

    calls = []
    original = rendering.render_notification
    monkeypatch.setattr(rendering, "render_notification", lambda *a, **k: calls.append(1) or original(*a, **k))
    set_profile(scenario.member, language="pt-br", zone="America/Sao_Paulo", name="Maria")
    set_profile(scenario.denied, language="pt-br", zone="America/Sao_Paulo", name="João")
    send(scenario, users=(scenario.member, scenario.denied), **PAYLOAD)
    assert len(calls) == 1
    assert row_of(scenario, scenario.member).body == row_of(scenario, scenario.denied).body


def test_the_link_of_the_text_opens_the_item_for_who_received_it(scenario):
    """#806: one address per notification; the route re-authorizes each click."""
    from skeleton.notifications.models import Notification
    from skeleton.notifications.preferences import set_preference

    with scenario.acting(scenario.denied, scenario.a):
        set_preference(scenario.denied, type_code="notes.linked", channel="in_app", mode="off", expected_revision=None)
    mail.outbox.clear()
    send(scenario, "notes.linked", users=(scenario.member, scenario.denied), title="x1")
    with system_context(reason="test:texts-link", organization_id=scenario.a.pk):
        notification = Notification.objects.get(type_code="notes.linked")
    path = reverse("notifications:item", kwargs={"organization_id": scenario.a.pk, "notification_id": notification.pk})
    row = row_of(scenario, scenario.member)
    assert row.body.startswith("Abra em ") and row.body.endswith(path)
    link = row.body.removeprefix("Abra em ")
    message = next(item for item in mail.outbox if scenario.denied.email in item.to)
    assert f'<a href="{link}">{link}</a>' in message.alternatives[0][0]
    member = support.login(scenario.member, password=FIXTURE_PASSWORD)
    opened = member.get(path)
    assert opened.status_code == 302 and opened["Location"] == "/item/x1/"
    assert row_of(scenario, scenario.member).read_at is not None
    only_email = support.login(scenario.denied, password=FIXTURE_PASSWORD)
    assert only_email.get(path)["Location"] == "/item/x1/"  # No inbox row: the email delivery counts.
    stranger = support.login(scenario.admin_a, password=FIXTURE_PASSWORD)
    page = stranger.get(path)
    assert page.status_code == 200 and "indisponível" in page.content.decode()
    outsider = support.login(scenario.admin_b, password=FIXTURE_PASSWORD)
    assert outsider.get(path).status_code in (403, 404)  # Not a member of A.
