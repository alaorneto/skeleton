"""The test kit of app themes and the example theme package (#645)."""

import shutil
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from skeleton.testing.appearance import assert_app_themes_valid
from tests.theme_package_support import app_theme_manifest, files, register_theme_app, unregister_theme_app

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "themes" / "caderno"


def test_the_kit_passes_a_valid_app_and_lists_every_problem(tmp_path):
    register_theme_app(tmp_path)
    try:
        assert_app_themes_valid("theme_demo")
    finally:
        unregister_theme_app()
    bad = {
        "aurora": files({"theme.css": b"a{}\n/* < */"}, manifest_value=app_theme_manifest()),
        "outro": files(
            manifest_value=app_theme_manifest(id="org.example.demo.theme.outro", tokens={"brand_color": "#ffff00"})
        ),
    }
    register_theme_app(tmp_path / "bad", themes=bad)
    try:
        with pytest.raises(AssertionError) as error:
            assert_app_themes_valid("theme_demo")
        assert "aurora:" in str(error.value) and "outro:" in str(error.value) and "Linha 2" in str(error.value)
    finally:
        unregister_theme_app()
    with pytest.raises(AssertionError, match="not registered"):
        assert_app_themes_valid("nada_aqui")


def test_the_example_theme_is_valid_as_a_package_and_as_an_app_theme(tmp_path):
    out = StringIO()
    call_command("appearance", "check-package", str(EXAMPLE), stdout=out)
    assert "org.example.theme.caderno 1.0.0" in out.getvalue() and "Pacote válido." in out.getvalue()
    copy = tmp_path / "theme_demo" / "themes" / "caderno"
    shutil.copytree(EXAMPLE, copy)
    text = (
        (copy / "theme.json")
        .read_text(encoding="utf-8")
        .replace("org.example.theme.caderno", "org.example.demo.theme.caderno")
    )
    (copy / "theme.json").write_text(text, encoding="utf-8")
    content = {path.relative_to(copy).as_posix(): path.read_bytes() for path in copy.rglob("*") if path.is_file()}
    register_theme_app(tmp_path / "app", themes={"caderno": content})
    try:
        assert_app_themes_valid("theme_demo")
    finally:
        unregister_theme_app()
