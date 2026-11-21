"""E047 (#642): an app that ships an invalid instance theme loads, and the check names the problem."""

import pytest

from skeleton.system.checks.app_themes import app_theme_checks
from tests.theme_package_support import app_theme_manifest, files, register_theme_app, unregister_theme_app


@pytest.fixture
def theme_app(tmp_path):
    created = []

    def make(**kwargs):
        manifest = register_theme_app(tmp_path, **kwargs)
        created.append(manifest.app_label)
        return manifest

    yield make
    for label in created:
        unregister_theme_app(label)


def problems():
    return [(error.id, error.obj, error.msg) for error in app_theme_checks()]


def test_a_valid_theme_has_no_error(theme_app):
    theme_app()
    assert problems() == []


def test_each_invalid_theme_is_an_error(theme_app, subtests):
    cases = {
        "css": files(
            {"theme.css": b"a{}\n/* < */"}, manifest_value=app_theme_manifest(id="org.example.demo.theme.css")
        ),
        "outro-id": files(manifest_value=app_theme_manifest(id="org.example.other.theme.x")),
        "nucleo": files(
            manifest_value=app_theme_manifest(id="org.example.demo.theme.nucleo", compatibility={"skeleton": "<0.0.1"})
        ),
    }
    cases["contraste"] = files(  # The id follows the rule, so only the contrast is wrong.
        manifest_value=app_theme_manifest(id="org.example.demo.theme.contraste", tokens={"brand_color": "#ffff00"})
    )
    theme_app(themes=cases)
    found = {message.split(" declara o tema ")[1].split(" ")[0]: message for _id, _obj, message in problems()}
    for slug, fragment in (
        ("contraste", "AA"),
        ("css", "Linha 2"),
        ("outro-id", "org.example.demo.theme.outro-id"),
        ("nucleo", "compatível"),
    ):
        with subtests.test(slug=slug):
            assert fragment in found[slug]
    assert {identifier for identifier, _obj, _msg in problems()} == {"skeleton.E047"}
    assert all("themes" not in message for message in found.values())  # Never a server path.
