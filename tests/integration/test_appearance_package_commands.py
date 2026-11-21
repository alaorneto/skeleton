"""The author console of theme packages (#634): check-package and build-package, without the database.

No test here asks for the database: pytest-django refuses any connection, which is the proof that
the two commands never open one.
"""

import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from tests.theme_package_support import files, make_zip, manifest, write_directory


def run(*args):
    out = StringIO()
    call_command("appearance", *args, stdout=out)
    return out.getvalue()


def failure(*args):
    with pytest.raises(CommandError) as error:
        run(*args)
    return str(error.value)


def test_check_package_directory_and_zip(tmp_path):
    source = write_directory(tmp_path / "aurora", files())
    output = run("check-package", str(source))
    assert "org.example.theme.aurora 1.0.0" in output and "Pacote válido." in output
    archive = tmp_path / "aurora.zip"
    archive.write_bytes(make_zip(files({"theme.css": b"a{}\n\n/* <style> */"})))
    message = failure("check-package", str(archive))
    assert "ERRO css" in message and "theme.css" in message and "Linha 3" in message


def test_check_package_refuses_contrast_and_core_range(tmp_path, subtests):
    with subtests.test(case="contrast"):
        source = write_directory(
            tmp_path / "amarelo", files(manifest_value=manifest(tokens={"brand_color": "#ffff00"}))
        )
        assert "ERRO brand_color" in failure("check-package", str(source))
    with subtests.test(case="core range"):
        source = write_directory(
            tmp_path / "velho", files(manifest_value=manifest(compatibility={"skeleton": "<0.0.1"}))
        )
        assert "ERRO incompatible_core" in failure("check-package", str(source))
    with subtests.test(case="missing"):
        assert "não existe" in failure("check-package", str(tmp_path / "nada"))


def test_build_package_is_deterministic_and_checked(tmp_path):
    source = write_directory(tmp_path / "aurora", files({"images/extra.txt": None}))
    first, second = tmp_path / "out" / "a.zip", tmp_path / "out" / "b.zip"
    output = run("build-package", str(source), "--output", str(first))
    run("build-package", str(source), "--output", str(second))
    assert first.read_bytes() == second.read_bytes()
    digest = json.dumps(output.split(" · ")[-1].split()[0])
    assert digest.strip('"') in run("check-package", str(first))
    assert "já existe" in failure("build-package", str(source), "--output", str(first))
    run("build-package", str(source), "--output", str(first), "--force")
    assert "dentro do diretório" in failure("build-package", str(source), "--output", str(source / "x.zip"))


def test_build_package_with_an_error_writes_nothing(tmp_path):
    source = write_directory(tmp_path / "ruim", files({"theme.css": b"@import 'x';"}))
    target = tmp_path / "ruim.zip"
    assert "ERRO css" in failure("build-package", str(source), "--output", str(target))
    assert not target.exists() and not list(tmp_path.glob(".theme-*"))
