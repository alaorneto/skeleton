"""The icon and the current entry of each app in the organization menu (#842)."""

import uuid
from dataclasses import replace

import pytest
from django.test import RequestFactory
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.system import app_registry
from skeleton.system.app_icons import icon_problems
from skeleton.system.app_registry import MenuEntry
from skeleton.system.applications import application_navigation, navigation_for_user
from skeleton.tenancy.context import system_context, tenant_context
from skeleton.testing.applications import activate_app, open_app
from tests import support

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults, pytest.mark.needs_example_app]
PASSWORD = "App-menu-icons-Password!842"
NOTES = "example_notes"
# A stroke icon of the app (a pencil), as a design tool exports it.
PENCIL = (
    '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">'
    '<path stroke-linecap="round" stroke-linejoin="round" d="m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652'
    'L6.832 19.82a4.5 4.5 0 0 1-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 0 1 1.13-1.897L16.863 4.487Z"/></svg>'
)
# The drawing of the core's cube, which already stands for the concept "apps".
CUBE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path d="m21 7.5-9-5.25L3 7.5m18 0-9 5.25m9-5.25v9l-9 5.25M3 7.5l9 5.25M3 7.5v9l9 5.25m0-9v9"/></svg>'
)


@pytest.fixture
def setup():
    from skeleton.access.models import Role, TablePermission
    from skeleton.system.models import TableDefinition

    owner, org = bootstrap_identity(
        email="menu-icons@example.test", organization="menu-icons", name="A", password=PASSWORD
    )
    with system_context(reason="test:menu-icons-demote"):
        owner.is_staff = owner.is_superuser = False
        owner.save(update_fields=["is_staff", "is_superuser"])
    open_app(NOTES)
    activate_app(NOTES, org.pk)
    with tenant_context(organization_id=org.pk, actor=owner, source="test:menu-icons-permission"):
        TablePermission.objects.get_or_create(
            organization=org,
            role=Role.objects.get(code="organization_admin", is_builtin=True),
            table=TableDefinition.objects.get(model_label="example_notes.note"),
            action="read",
        )
    return owner, org


def declare(monkeypatch, tmp_path, *entries, files=None):
    for name, content in (files or {}).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    manifest = replace(app_registry.get_app(NOTES), navigation=entries, root=str(tmp_path))
    monkeypatch.setitem(app_registry._manifests, NOTES, manifest)


def menu(owner, org):
    with tenant_context(organization_id=org.pk, actor=owner, source="test:menu-icons"):
        return [item for item in navigation_for_user(owner) if item["app_label"] == NOTES]


def test_each_entry_draws_its_declared_icon(setup, monkeypatch, tmp_path, subtests):
    owner, org = setup
    entry = MenuEntry("Notas", "example_notes:home", "example_notes.note")
    with subtests.test(case="default"):
        declare(monkeypatch, tmp_path, entry)
        assert [(item["concept"], item["icon_svg"]) for item in menu(owner, org)] == [("apps", "")]
    with subtests.test(case="concept"):
        declare(monkeypatch, tmp_path, replace(entry, icon="calendar"))
        assert [(item["concept"], item["icon_svg"]) for item in menu(owner, org)] == [("calendar", "")]
        assert icon_problems() == []
    with subtests.test(case="own svg"):
        declare(monkeypatch, tmp_path, replace(entry, icon_svg="icons/pencil.svg"), files={"icons/pencil.svg": PENCIL})
        [item] = menu(owner, org)
        assert item["icon_svg"].startswith('<path d="m16.862 4.487') and "stroke-linecap" not in item["icon_svg"]
        assert icon_problems() == []
        page = support.login(owner, password=PASSWORD, csrf=False).get(reverse("web:dashboard", args=[org.pk]))
        assert '<path d="m16.862 4.487' in page.content.decode()


def test_an_icon_the_menu_cannot_draw_keeps_the_default_and_is_reported(setup, monkeypatch, tmp_path, subtests):
    owner, org = setup
    entry = MenuEntry("Notas", "example_notes:home", "example_notes.note")
    cases = [
        ("unknown concept", replace(entry, icon="nope"), {}, "o conceito nope não existe"),
        ("core icon", replace(entry, icon_svg="icons/cube.svg"), {"icons/cube.svg": CUBE}, "ícone cube do núcleo"),
        ("missing file", replace(entry, icon_svg="icons/none.svg"), {}, "não existe no pacote"),
        (
            "active content",
            replace(entry, icon_svg="icons/x.svg"),
            {"icons/x.svg": PENCIL.replace("<path", "<script/><path")},
            "element (script)",
        ),
        ("escapes the package", replace(entry, icon_svg="../outside.svg"), {}, "não existe no pacote"),
    ]
    for case, declared, files, problem in cases:
        with subtests.test(case=case):
            (tmp_path.parent / "outside.svg").write_text(PENCIL, encoding="utf-8")
            declare(monkeypatch, tmp_path, declared, files=files)
            assert [(item["concept"], item["icon_svg"]) for item in menu(owner, org)] == [("apps", "")]
            [(app_label, label, message)] = icon_problems()
            assert (app_label, label) == (NOTES, "Notas") and problem in message, message


def test_two_apps_never_share_an_svg(setup, monkeypatch, tmp_path):
    owner, org = setup
    entry = MenuEntry("Notas", "example_notes:home", "example_notes.note", icon_svg="icons/pencil.svg")
    declare(monkeypatch, tmp_path, entry, files={"icons/pencil.svg": PENCIL})
    twin = replace(app_registry.get_app(NOTES), app_label="menu_icon_twin", app_id="menu_icon_twin", package_name=None)
    monkeypatch.setitem(app_registry._manifests, "menu_icon_twin", twin)
    open_app("menu_icon_twin")
    activate_app("menu_icon_twin", org.pk)
    problems = icon_problems()
    assert {(app_label, "app" in message) for app_label, _, message in problems} == {
        (NOTES, True),
        ("menu_icon_twin", True),
    }
    with tenant_context(organization_id=org.pk, actor=owner, source="test:menu-icons-twin"):
        assert {item["icon_svg"] for item in navigation_for_user(owner)} == {""}


def test_the_entry_of_the_app_is_current_on_every_page_of_it(setup):
    owner, org = setup
    home = reverse("example_notes:home", kwargs={"organization_id": org.pk})
    subpage = reverse("example_notes:request-publication", kwargs={"organization_id": org.pk, "note_id": uuid.uuid4()})
    for path, current in ((home, True), (subpage, True), (reverse("web:dashboard", args=[org.pk]), False)):
        request = RequestFactory().get(path)
        request.user = owner
        with tenant_context(organization_id=org.pk, actor=owner, source="test:menu-icons-current"):
            entries = application_navigation(request)["application_navigation"]
        assert [entry["current"] for entry in entries if entry["app_label"] == NOTES] == [current], path
