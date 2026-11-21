"""The theme catalog of the instance (#595): CRUD with CAS, activation, limits, audit and reset-theme."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import connection, connections

from skeleton.common.exceptions import ConcurrentUpdate
from tests.integration.appearance_support import acting, audit_events, member, platform_admin

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")
HIDE_ALL = "body{display:none}"


def appearance(user):
    from skeleton.appearance.snapshot import current_appearance

    with acting(user):
        return current_appearance()


def test_create_edit_activate_and_delete_with_audit():
    from skeleton.appearance.models import InstanceTheme
    from skeleton.appearance.services import (
        activate_theme,
        create_theme,
        current_brand,
        delete_theme,
        list_themes,
        revision_of,
        update_theme,
    )

    admin = platform_admin()
    data = {"name": "Mar", "brand_color": "#0F766E", "radius": "round", "font": "serif", "custom_css": HIDE_ALL}
    with acting(admin):
        theme = create_theme(actor=admin, data=data)
        other = create_theme(actor=admin, data={"name": "Outro"})
    assert theme.brand_color == "#0f766e"
    with acting(admin):
        theme = update_theme(actor=admin, theme_id=theme.pk, data={"radius": "sharp"}, expected_revision=1)
        assert theme.revision == 2
        activate_theme(actor=admin, theme_id=theme.pk, expected_revision=0)
    current = appearance(admin)
    assert HIDE_ALL in current.theme_css and HIDE_ALL not in current.theme_css_tokens
    assert current.theme_css_tokens and current.theme_version and current.theme_version != current.theme_safe_version
    assert current.brand_color == "#0f766e"
    with acting(admin):
        activate_theme(actor=admin, builtin="vinho", expected_revision=revision_of(current_brand()))
    assert appearance(admin).brand_color == "#9f1239"
    assert appearance(admin).theme_version != current.theme_version
    with acting(admin):
        delete_theme(actor=admin, theme_id=theme.pk, expected_revision=2)
        assert [item.name for item in list_themes()] == ["Outro"]
        assert InstanceTheme.objects.with_deleted().filter(pk=theme.pk, deleted_at__isnull=False).exists()
        create_theme(actor=admin, data={"name": "Mar"})  # The name is free again.
        assert other.pk in {item.pk for item in list_themes()}
    events = audit_events(admin, theme.pk)
    assert [event.operation for event in events] == ["create", "update", "soft_delete"]
    assert HIDE_ALL in str(events[0].changes)  # The CSS is not sensitive: the audit shows who changed what.
    brand_events = audit_events(admin, current_brand().pk)
    assert len(brand_events) == 2 and all(event.actor_sysid == admin.pk for event in brand_events)


def test_only_the_platform_administrator_writes(subtests):
    from skeleton.appearance import services

    admin = platform_admin()
    with acting(admin):
        theme = services.create_theme(actor=admin, data={"name": "Base"})
    other = member()
    calls = {
        "create": lambda: services.create_theme(actor=other, data={"name": "X"}),
        "update": lambda: services.update_theme(
            actor=other, theme_id=theme.pk, data={"name": "X"}, expected_revision=1
        ),
        "delete": lambda: services.delete_theme(actor=other, theme_id=theme.pk, expected_revision=1),
        "activate": lambda: services.activate_theme(actor=other, theme_id=theme.pk, expected_revision=0),
    }
    with acting(other):
        for name, call in calls.items():
            with subtests.test(call=name), pytest.raises(PermissionDenied):
                call()


def test_cas_on_edit_delete_and_activate(subtests):
    from skeleton.appearance import services

    admin = platform_admin()
    with acting(admin):
        theme = services.create_theme(actor=admin, data={"name": "Base"})
        services.activate_theme(actor=admin, builtin="grafite", expected_revision=0)
        calls = {
            "update": lambda: services.update_theme(
                actor=admin, theme_id=theme.pk, data={"font": "system"}, expected_revision=5
            ),
            "delete": lambda: services.delete_theme(actor=admin, theme_id=theme.pk, expected_revision=0),
            "activate": lambda: services.activate_theme(actor=admin, theme_id=theme.pk, expected_revision=0),
            "activate-text": lambda: services.activate_theme(actor=admin, builtin="vinho", expected_revision="1"),
        }
        for name, call in calls.items():
            with subtests.test(call=name), pytest.raises(ConcurrentUpdate):
                call()


def test_invalid_values_name_the_field(subtests):
    from skeleton.appearance import services

    admin = platform_admin()
    with acting(admin):
        services.create_theme(actor=admin, data={"name": "Existente"})
        cases = [
            ({"name": "Existente"}, "name"),
            ({"name": "  "}, "name"),
            ({"name": "x" * 81}, "name"),
            ({"name": "a\nb"}, "name"),
            ({"name": "Cor", "brand_color": "#ffff00"}, "brand_color"),
            ({"name": "Cor", "brand_color": "azul"}, "brand_color"),
            ({"name": "Raio", "radius": "huge"}, "radius"),
            ({"name": "Fonte", "font": "comic"}, "font"),
            ({"name": "Css", "custom_css": "@import 'x';"}, "custom_css"),
            ({"name": "Css", "custom_css": ".a{background:url(//x)}"}, "custom_css"),
            ({"name": "Extra", "sysid": "x"}, "sysid"),
        ]
        for data, field in cases:
            with subtests.test(field=field, data=repr(data)[:40]):
                with pytest.raises(ValidationError) as error:
                    services.create_theme(actor=admin, data=data)
                assert field in error.value.message_dict
        with subtests.test(case="unknown builtin"), pytest.raises(ValidationError):
            services.activate_theme(actor=admin, builtin="inexistente", expected_revision=0)


def test_the_51st_theme_is_refused(monkeypatch):
    from skeleton.appearance import services

    monkeypatch.setattr(services, "MAX_THEMES", 3)
    admin = platform_admin()
    with acting(admin):
        for index in range(3):
            services.create_theme(actor=admin, data={"name": f"Tema {index}"})
        with pytest.raises(ValidationError):
            services.create_theme(actor=admin, data={"name": "Sobra"})


def test_the_active_theme_cannot_be_deleted():
    from skeleton.appearance import services

    admin = platform_admin()
    with acting(admin):
        theme = services.create_theme(actor=admin, data={"name": "Ativo"})
        services.activate_theme(actor=admin, theme_id=theme.pk, expected_revision=0)
        with pytest.raises(ValidationError) as error:
            services.delete_theme(actor=admin, theme_id=theme.pk, expected_revision=1)
        assert "theme" in error.value.message_dict
        assert [item.pk for item in services.list_themes()] == [theme.pk]


def test_two_activations_with_the_same_revision_have_one_winner():
    from skeleton.appearance import services

    connection.ensure_connection()
    admin = platform_admin()
    with acting(admin):
        services.activate_theme(actor=admin, builtin="grafite", expected_revision=0)
    barrier = Barrier(2)

    def activate(slug):
        connections.close_all()
        try:
            with acting(admin):
                barrier.wait(10)
                try:
                    services.activate_theme(actor=admin, builtin=slug, expected_revision=1)
                except ConcurrentUpdate:
                    return "lost"
                return "won"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(activate, ["vinho", "violeta"], timeout=60)) == ["lost", "won"]


def test_reset_theme_goes_back_to_the_core_theme():
    from skeleton.appearance import services

    admin = platform_admin()
    with acting(admin):
        theme = services.create_theme(actor=admin, data={"name": "Escondido", "custom_css": HIDE_ALL})
        services.activate_theme(actor=admin, theme_id=theme.pk, expected_revision=0)
    assert HIDE_ALL in appearance(admin).theme_css
    call_command("appearance", "reset-theme")
    current = appearance(admin)
    assert current.theme_css == "" and current.theme_version == ""
    with acting(admin):
        brand = services.current_brand()
        assert brand.active_theme_id is None and brand.builtin_theme == ""
    event = audit_events(admin, brand.pk)[-1]
    assert event.actor_sysid is None and "appearance.reset_theme" in event.source


def test_a_deleted_or_unknown_active_theme_is_the_core_theme(subtests):
    from django.db import transaction

    from skeleton.appearance import services
    from skeleton.appearance.snapshot import reset_appearance_cache

    admin = platform_admin()
    with acting(admin):
        services.activate_theme(actor=admin, builtin="vinho", expected_revision=0)
    with acting(admin), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE skeleton.sk_appearance_instancebrand SET builtin_theme = 'sumiu', revision = revision + 1"
        )
    reset_appearance_cache()
    with subtests.test(case="unknown slug"):
        assert appearance(admin).theme_css == ""
