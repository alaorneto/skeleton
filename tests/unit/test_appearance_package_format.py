"""The theme package format 1 and its reader (#633): everything validated in memory, nothing written."""

import io
import json
import os
import random
import subprocess
import sys
import zipfile

import pytest

from skeleton.appearance.package_format import (
    ERROR_CODES,
    MAX_ARCHIVE_BYTES,
    ThemePackageError,
    check_core_compatibility,
    read_theme_archive,
    read_theme_directory,
)
from tests.theme_package_support import FONT, files, make_zip, manifest, png, theme_zip, write_directory


def refused(data):
    with pytest.raises(ThemePackageError) as error:
        read_theme_archive(data)
    assert error.value.code in ERROR_CODES
    return error.value


def test_the_full_example_is_read():
    package = read_theme_archive(theme_zip())
    assert (package.id, package.version, package.name) == ("org.example.theme.aurora", "1.0.0", "Aurora")
    assert (package.brand_color, package.radius, package.font, package.font_family) == (
        "#1d4ed8",
        "round",
        "default",
        "Aurora Sans",
    )
    assert (package.product_name, package.footer_text) == ("Aurora", "Aurora · atendimento ao cliente")
    assert [image.kind for image in package.images] == ["logo", "logo_dark", "icon"]
    assert [image.mime for image in package.images] == ["image/svg+xml", "image/png", "image/png"]
    assert package.fonts[0].weight == "100 900" and package.fonts[0].content == FONT
    assert "--acme-accent" in package.custom_css
    assert package.manifest["css"] == "theme.css" and len(package.content_digest) == 64


def test_defaults_are_filled_in_the_manifest():
    minimal = {"format": 1, "id": "org.example.theme.min", "version": "0.1.0", "name": "Min"}
    minimal["compatibility"] = {"skeleton": ">=0.0.0"}
    package = read_theme_archive(make_zip({"theme.json": json.dumps(minimal).encode()}))
    assert package.manifest["tokens"] == {"brand_color": "", "radius": "default", "font": "default", "font_family": ""}
    assert "css" not in package.manifest and package.custom_css == "" and package.images == ()
    with_css = make_zip({"theme.json": json.dumps(minimal).encode(), "theme.css": b".a{}"})
    assert read_theme_archive(with_css).manifest["css"] == "theme.css"


def test_digest_is_the_content_not_the_zip(tmp_path):
    first = read_theme_archive(theme_zip())
    stored = io.BytesIO()
    with zipfile.ZipFile(stream := stored, "w", zipfile.ZIP_STORED) as archive:
        for path, data in reversed(list(files().items())):
            archive.writestr(path, data)
    assert read_theme_archive(stream.getvalue()).content_digest == first.content_digest
    assert read_theme_directory(write_directory(tmp_path, files())).content_digest == first.content_digest
    assert read_theme_archive(theme_zip({"README.md": b"outro"})).content_digest == first.content_digest
    assert read_theme_archive(theme_zip({"theme.css": b".b{}"})).content_digest != first.content_digest
    changed = read_theme_archive(theme_zip({"fonts/aurora-sans.woff2": FONT + b"x"}))
    assert changed.content_digest != first.content_digest


def with_manifest(**changes):
    return theme_zip(manifest_value=manifest(**changes))


CASES = [
    ("format", with_manifest(format=2), "format"),
    ("format bool", with_manifest(format=True), "format"),
    ("id", with_manifest(id="Org Example"), "id"),
    ("version", with_manifest(version="um"), "version"),
    ("name empty", with_manifest(name="  "), "name"),
    ("name long", with_manifest(name="x" * 81), "name"),
    ("name newline", with_manifest(name="a\nb"), "name"),
    ("description", with_manifest(description="x" * 301), "text_field"),
    ("publisher", with_manifest(publisher="X Y"), "publisher"),
    ("compatibility", with_manifest(compatibility={"skeleton": "qualquer"}), "compatibility"),
    ("compatibility extra", with_manifest(compatibility={"skeleton": ">=0", "x": 1}), "unknown_field"),
    ("missing id", with_manifest(id=None), "manifest_field"),
    ("unknown top", with_manifest(theme="x"), "unknown_field"),
    ("unknown token", with_manifest(tokens={"color": "#000000"}), "unknown_field"),
    ("brand color", with_manifest(tokens={"brand_color": "azul"}), "brand_color"),
    ("radius", with_manifest(tokens={"radius": "pill"}), "radius"),
    ("font", with_manifest(tokens={"font": "comic"}), "font"),
    ("font family undeclared", with_manifest(tokens={"font_family": "Outra"}), "font_family"),
    ("product name", with_manifest(brand={"product_name": "x" * 61}), "text_field"),
    ("footer", with_manifest(brand={"footer_text": "a\rb"}), "text_field"),
    ("image outside", with_manifest(images={"logo": "logo.svg"}), "image_path"),
    ("image missing", with_manifest(images={"logo": "images/nada.png"}), "image_path"),
    ("logo dark alone", with_manifest(images={"logo_dark": "images/logo-dark.png"}), "logo_dark_without_logo"),
    (
        "logo gif",
        theme_zip({"images/logo.gif": b"GIF89a"}, manifest_value=manifest(images={"logo": "images/logo.gif"})),
        "image_path",
    ),
    (
        "svg script",
        theme_zip({"images/logo.svg": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><script/></svg>'}),
        "svg",
    ),
    ("raster not png", theme_zip({"images/logo-dark.png": b"texto"}), "image_type"),
    (
        "raster large",
        theme_zip({"images/logo-dark.png": png(64, 32) + random.Random(1).randbytes(600_000)}),
        "image_size",
    ),
    ("icon size", theme_zip({"images/icon.png": png(256, 256)}), "icon"),
    (
        "too many fonts",
        with_manifest(fonts=[{"family": f"F{i}x", "file": "fonts/aurora-sans.woff2"} for i in range(9)]),
        "too_many_fonts",
    ),
    (
        "generic family",
        with_manifest(tokens={}, fonts=[{"family": "Serif", "file": "fonts/aurora-sans.woff2"}]),
        "font_family_name",
    ),
    ("font not woff2", theme_zip({"fonts/aurora-sans.woff2": b"OTTO" + b"\0" * 10}), "font_file"),
    (
        "font weight",
        with_manifest(fonts=[{"family": "Aurora Sans", "file": "fonts/aurora-sans.woff2", "weight": "450"}]),
        "font_weight",
    ),
    (
        "font range",
        with_manifest(fonts=[{"family": "Aurora Sans", "file": "fonts/aurora-sans.woff2", "weight": "900 100"}]),
        "font_weight",
    ),
    (
        "font style",
        with_manifest(fonts=[{"family": "Aurora Sans", "file": "fonts/aurora-sans.woff2", "style": "oblique"}]),
        "font_style",
    ),
    ("css declared missing", theme_zip({"theme.css": None}), "css_missing"),
    ("css markup", theme_zip({"theme.css": b"a{}\n/* <style> */"}), "css"),
    ("unexpected", theme_zip({"images/sobra.png": png(1, 1)}), "unexpected_file"),
    ("hidden", theme_zip({".DS_Store": b"x"}), "unexpected_file"),
    ("upper case", theme_zip({"images/Extra.svg": b"x"}), "unexpected_file"),
    ("no manifest", theme_zip({"theme.json": None}), "missing_manifest"),
    ("not json", theme_zip({"theme.json": b"{"}), "manifest_json"),
    ("duplicate key", theme_zip({"theme.json": b'{"format":1,"format":1}'}), "manifest_json"),
    ("not object", theme_zip({"theme.json": b"[]"}), "manifest_json"),
    ("not zip", b"not a zip", "archive"),
]


def test_each_rule_has_its_code(subtests):
    for case, data, code in CASES:
        with subtests.test(case=case):
            assert refused(data).code == code


def test_paths_lines_and_items_are_reported():
    error = refused(theme_zip({"theme.css": b"a{}\n/* <style> */"}))
    assert (error.path, error.line, error.item) == ("theme.css", 2, "markup")
    error = refused(theme_zip({"images/sobra.png": png(1, 1)}))
    assert error.path == "images/sobra.png"
    error = refused(
        theme_zip({"images/logo.svg": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><script/></svg>'})
    )
    assert (error.path, error.item) == ("images/logo.svg", "element")


def test_archive_limits():
    assert refused(make_zip(files(), folder="aurora/")).code == "missing_manifest"
    many = files({f"LICENSES/l{i}.txt": b"x" for i in range(40)})
    assert refused(make_zip(many)).code == "too_many_files"
    assert refused(b"\0" * (MAX_ARCHIVE_BYTES + 1)).code == "too_large"
    bomb = theme_zip({"theme.css": b" " * 5_000_000})  # Compresses far beyond 100:1.
    assert refused(bomb).code == "archive"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for path, data in files().items():
            archive.writestr(path, data)
        archive.writestr("../escape.txt", b"x")
    assert refused(stream.getvalue()).code in ("archive", "unexpected_file")


def test_directory_reader(tmp_path, subtests):
    root = write_directory(tmp_path / "tema", files())
    assert read_theme_directory(root).id == "org.example.theme.aurora"
    with subtests.test(case="missing directory"):
        with pytest.raises(ThemePackageError) as error:
            read_theme_directory(tmp_path / "nada")
        assert error.value.code == "missing_manifest"
    with subtests.test(case="hidden file"):
        (root / ".git").write_bytes(b"x")
        with pytest.raises(ThemePackageError) as error:
            read_theme_directory(root)
        assert error.value.code == "unexpected_file"
        (root / ".git").unlink()
    with subtests.test(case="symbolic link"):
        try:
            os.symlink(root / "theme.css", root / "images" / "link.css")
        except (OSError, NotImplementedError):
            pytest.skip("Symbolic links are not allowed on this machine.")
        with pytest.raises(ThemePackageError) as error:
            read_theme_directory(root)
        assert error.value.code == "archive"


def test_core_compatibility():
    package = read_theme_archive(with_manifest(compatibility={"skeleton": ">=0.2.0,<0.3.0"}))
    check_core_compatibility(package, "0.2.0")
    for version in ("0.3.0", None):
        with pytest.raises(ThemePackageError) as error:
            check_core_compatibility(package, version)
        assert error.value.code == "incompatible_core"


def test_the_reader_does_not_import_django():
    environment = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    code = "import sys, skeleton.appearance.package_format; print('django' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=environment, check=True)
    assert result.stdout.strip() == "False"


def test_the_legacy_name_of_the_core_font_is_read_as_the_default():
    """#690: packages made for older cores say "inter"; they keep working, read as "default"."""
    legacy = {"format": 1, "id": "org.example.theme.old", "version": "0.1.0", "name": "Old"}
    legacy["compatibility"] = {"skeleton": ">=0.0.0"}
    legacy["tokens"] = {"font": "inter"}
    package = read_theme_archive(make_zip({"theme.json": json.dumps(legacy).encode()}))
    assert package.font == "default" and package.manifest["tokens"]["font"] == "default"
    legacy["tokens"] = {"font": "Inter"}  # Only the exact legacy name; anything else is refused.
    with pytest.raises(ThemePackageError) as refused:
        read_theme_archive(make_zip({"theme.json": json.dumps(legacy).encode()}))
    assert refused.value.code == "font"


def test_a_package_may_name_the_core_family_or_the_old_one():
    """#690: no family is reserved but the generic CSS keywords: a package that ships its own
    IBM Plex Sans (or Inter) under the usual name keeps installing on every core."""
    for family in ("IBM Plex Sans Variable", "Inter Variable"):
        tokens = {"font_family": family}
        fonts = [{"family": family, "file": "fonts/aurora-sans.woff2", "weight": "100 900"}]
        package = read_theme_archive(with_manifest(tokens=tokens, fonts=fonts))
        assert package.font_family == family and package.fonts[0].family == family
