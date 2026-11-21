"""The deterministic zip of a theme package and the messages of every error code (#634)."""

import zipfile
from io import BytesIO

from skeleton.appearance.package_build import FIXED_DATE, build_theme_zip, package_files
from skeleton.appearance.package_format import ERROR_CODES, from_files, read_theme_archive
from skeleton.appearance.package_messages import PACKAGE_ERROR_MESSAGES, package_error_message
from tests.theme_package_support import files


def test_only_used_files_in_order_with_fixed_metadata():
    source = files({"notes/rascunho.txt": b"x"})
    chosen = package_files(from_files({k: v for k, v in source.items() if k != "notes/rascunho.txt"}), source)
    data = build_theme_zip(chosen)
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        assert names == sorted(names) and "notes/rascunho.txt" not in names
        assert {"README.md", "LICENSES/OFL.txt", "theme.json", "fonts/aurora-sans.woff2"} <= set(names)
        assert all(info.date_time == FIXED_DATE and info.external_attr >> 16 == 0o100644 for info in archive.infolist())
    assert build_theme_zip(dict(reversed(list(chosen.items())))) == data
    assert read_theme_archive(data).content_digest == from_files(chosen).content_digest


def test_every_error_code_has_a_message(subtests):
    for code in ERROR_CODES:
        with subtests.test(code=code):
            assert code in PACKAGE_ERROR_MESSAGES
            assert package_error_message(code, path="images/x.png", line=2, item="url(")


def test_css_messages_name_the_file_and_the_line():
    assert package_error_message("css", path="theme.css", line=3, item="url(").startswith("theme.css: Linha 3:")
    assert "comentários" in package_error_message("css", path="theme.css", line=1, item="markup")
