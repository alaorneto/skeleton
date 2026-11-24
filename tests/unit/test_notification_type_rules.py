"""skeleton.E023: context, audience, fields and factory text of notification types (#782)."""

from django.utils.translation import gettext_lazy

from skeleton.notifications.registry import (
    NotificationField,
    NotificationType,
    all_notification_types,
    notification_problems,
    org_can_disable,
    org_can_edit_text,
    register_notification_type,
    type_context,
)
from skeleton.notifications.texts import validate
from skeleton.testing.registries import isolated_notification_types


def problems_of(*kinds):
    with isolated_notification_types():
        for kind in kinds:
            register_notification_type(kind)
        return notification_problems()


def test_context_is_derived_for_apps_and_declared_by_the_core():
    app = NotificationType("tasks.done", "Feita", owner_app="tasks")
    core = NotificationType("x.y", "Y", owner_app="sk_tenancy", context="organization")
    assert type_context(app) == "app"
    assert type_context(core) == "organization"
    assert problems_of(app, core) == []
    assert problems_of(NotificationType("x.z", "Z", owner_app="sk_tenancy")) == [
        "x.z: a core type declares its context"
    ]
    assert problems_of(NotificationType("t.p", "P", owner_app="tasks", context="platform")) == [
        "t.p: an app type has the app context"
    ]
    assert problems_of(NotificationType("t.q", "Q", owner_app="tasks", context="other")) == [
        "t.q: context is platform, organization or app"
    ]


def test_platform_audience_rules():
    good = NotificationType(
        "platform.x",
        "X",
        owner_app="sk_jobs",
        context="platform",
        audience="platform",
        platform_capability="updates.operate",
    )
    assert problems_of(good) == []
    problems = problems_of(
        NotificationType("p.a", "A", owner_app="sk_jobs", context="organization", audience="platform"),
        NotificationType(
            "p.b", "B", owner_app="sk_jobs", context="platform", audience="platform", default_channels=("push",)
        ),
        NotificationType(
            "p.c", "C", owner_app="sk_jobs", context="platform", audience="platform", platform_capability="nope"
        ),
        NotificationType("p.d", "D", owner_app="sk_jobs", context="platform", platform_capability="updates.operate"),
    )
    assert "p.a: a platform audience needs the platform context" in problems
    assert "p.b: a platform type uses only the in_app and email channels" in problems
    assert "p.c: unknown platform capability nope" in problems
    assert "p.d: platform_capability needs the platform audience" in problems


def test_fields_must_be_declared_and_coherent():
    problems = problems_of(
        NotificationType(
            "f.a",
            "A",
            owner_app="tasks",
            payload_fields=("total", "who"),
            fields=(
                NotificationField("missing", "M"),
                NotificationField("total", "T", kind="money"),
                NotificationField("who", "W", kind="weird"),
            ),
        )
    )
    assert "f.a: text field missing is not a payload or snapshot field" in problems
    assert "f.a: money field total needs a declared currency_field" in problems
    assert "f.a: text field who has an unknown kind weird" in problems


def test_a_field_may_not_take_the_name_of_a_core_placeholder():
    problems = problems_of(
        NotificationType(
            "f.b",
            "B",
            owner_app="tasks",
            payload_fields=("destinatario", "data_atual", "due"),
            fields=(
                NotificationField("destinatario", "D"),
                NotificationField("data_atual", "A", kind="date"),
                NotificationField("due", "Prazo", kind="date"),
            ),
        )
    )
    assert problems == [
        "f.b: text field destinatario is a placeholder of the core",
        "f.b: text field data_atual is a placeholder of the core",
    ]


def test_factory_text_is_validated():
    problems = problems_of(
        NotificationType("t.a", "A", owner_app="tasks", title="Olá {who}"),
        NotificationType("t.b", "B", owner_app="tasks", body="Só corpo"),
        NotificationType("t.c", "C", owner_app="tasks", title=gettext_lazy("Chave {solta")),
    )
    assert any(item.startswith("t.a: factory title is invalid") for item in problems)
    assert "t.b: a factory body needs a title" in problems
    assert any(item.startswith("t.c: factory title is invalid") for item in problems)
    assert problems_of(NotificationType("t.d", "D", owner_app="tasks", title="{aviso} em {organizacao}")) == []
    fixed = "Olá, {destinatario_primeiro_nome} ({destinatario}): {plataforma} em {data_atual}"
    assert problems_of(NotificationType("t.f", "F", owner_app="tasks", title=fixed)) == []


def test_fixed_text_belongs_to_the_platform():
    assert problems_of(NotificationType("t.e", "E", owner_app="tasks", fixed_text=True)) == [
        "t.e: a fixed text belongs to a platform type"
    ]


def test_what_the_organization_may_change():
    platform = NotificationType("p.x", "X", owner_app="sk_billing", context="platform")
    mandatory = NotificationType("o.m", "M", owner_app="sk_tenancy", context="organization", mandatory=True)
    locked_text = NotificationType("a.l", "L", owner_app="tasks", org_editable=False)
    assert not org_can_edit_text(platform) and not org_can_disable(platform)
    assert org_can_edit_text(mandatory) and not org_can_disable(mandatory)
    assert not org_can_edit_text(locked_text) and org_can_disable(locked_text)


def test_every_registered_type_has_a_context_and_a_valid_factory_text():
    """The real registry after django.setup(): no type without a context, every text valid."""
    from skeleton.notifications.registry import text_fields

    kinds = all_notification_types()
    assert kinds
    for kind in kinds:
        assert type_context(kind), kind.code
        for part in ("title", "body"):
            text = str(getattr(kind, part) or "")
            if text:
                validate(text, allowed=text_fields(kind), kind=part)
