"""Instance themes in the manifest of an app (#642): declaration, floor, build and doctor."""

import json
import os
import subprocess
import sys

import pytest

from skeleton.system.packages import PackageError, canonical_json, doctor, manifest_template, validate_manifest
from tests.theme_package_support import files, write_directory
from tests.theme_package_support import manifest as theme_manifest

APP_ID = "org.test.demo"


@pytest.fixture
def manifest():
    value = manifest_template(
        app_id=APP_ID,
        app_label="project_demo",
        package_name="package_demo",
        publisher="test-publisher",
        app_config_class="DemoConfig",
        service_classes=["DemoService"],
    )
    value["compatibility"]["skeleton"] = ">=0.3.0,<1.0.0"
    return value


def validated(value):
    return validate_manifest(json.loads(canonical_json(value)))


def themes(value, entries):
    value = json.loads(canonical_json(value))
    value["runtime"]["instance_themes"] = entries
    return value


def test_a_valid_declaration(manifest):
    value = themes(manifest, [{"slug": "aurora", "path": "themes/aurora"}, {"slug": "noite", "path": "themes/noite"}])
    assert validated(value)["runtime"]["instance_themes"][0] == {"slug": "aurora", "path": "themes/aurora"}


def test_refusals(manifest, subtests):
    cases = [
        ("empty", [], "1 to 4"),
        ("five", [{"slug": f"t{i}", "path": f"themes/t{i}"} for i in range(5)], "1 to 4"),
        ("bad slug", [{"slug": "Aurora", "path": "themes/a"}], "unique slugs"),
        ("repeated slug", [{"slug": "a", "path": "themes/a"}, {"slug": "a", "path": "themes/b"}], "unique slugs"),
        ("extra key", [{"slug": "a", "path": "themes/a", "x": 1}], ""),
        ("unsafe path", [{"slug": "a", "path": "../fora"}], "Unsafe"),
        ("absolute", [{"slug": "a", "path": "/tmp/a"}], "Unsafe"),
        ("role directory", [{"slug": "a", "path": "static/tema"}], "cannot live"),
        ("nested", [{"slug": "a", "path": "themes/a"}, {"slug": "b", "path": "themes/a/b"}], "never nested"),
        ("same path", [{"slug": "a", "path": "themes/a"}, {"slug": "b", "path": "themes/a"}], "never nested"),
    ]
    for case, entries, message in cases:
        with subtests.test(case=case):
            with pytest.raises(PackageError, match=message):
                validated(themes(manifest, entries))
    with subtests.test(case="floor"):
        low = themes(manifest, [{"slug": "a", "path": "themes/a"}])
        low["compatibility"]["skeleton"] = ">=0.2.0,<1.0.0"
        with pytest.raises(PackageError, match="instance_themes requires a core minimum of 0.3.0"):
            validated(low)
    with subtests.test(case="manifest v1"):
        old = themes(manifest, [{"slug": "a", "path": "themes/a"}])
        old["manifest_version"] = 1
        with pytest.raises(PackageError):
            validated(old)


def source(tmp_path, manifest, theme_files=None, theme=None):
    """A minimal app source that ships one theme (the doctor reads it, nothing is imported)."""
    root = tmp_path / "source"
    (root / "docs").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "apps.py").write_text(
        "from django.apps import AppConfig\nclass DemoConfig(AppConfig):\n"
        '    name = __package__\n    label = "project_demo"\n'
    )
    (root / "service_api.py").write_text("class DemoService:\n    pass\n")
    (root / "docs/spec.md").write_text("## Contract\nX.\n## Acceptance\nX.\n")
    (root / "docs/adr.md").write_text("## Context\nX.\n## Decision\nX.\n## Consequences\nX.\n")
    (root / "docs/service-api.md").write_text(
        "## Serviços\nX.\n## DTOs\nX.\n## Histórico de versões\nX.\n", encoding="utf-8"
    )
    value = themes(manifest, [{"slug": "aurora", "path": "themes/aurora"}])
    content = theme_files or files(
        manifest_value=theme or theme_manifest(id=f"{APP_ID}.theme.aurora", compatibility={"skeleton": ">=0.3.0"})
    )
    write_directory(root / "themes" / "aurora", content)
    (root / "manifest.json").write_bytes(canonical_json(value))
    return root


def theme_codes(result):
    return [
        (problem["theme"], problem["theme_code"])
        for problem in result["problems"]
        if problem["code"] == "instance_theme_invalid"
    ]


def test_doctor_reports_each_theme_problem(tmp_path, manifest, subtests):
    with subtests.test(case="valid"):
        assert theme_codes(doctor(source(tmp_path / "ok", manifest))) == []
    with subtests.test(case="bad css"):
        root = source(tmp_path / "css", manifest, files({"theme.css": b"a{}\n/* < */"}))
        assert theme_codes(doctor(root)) == [("aurora", "css")]
    with subtests.test(case="id outside the rule"):
        root = source(tmp_path / "id", manifest, theme=theme_manifest(compatibility={"skeleton": ">=0.3.0"}))
        assert theme_codes(doctor(root)) == [("aurora", "theme_id")]
    with subtests.test(case="ranges do not meet"):
        theme = theme_manifest(id=f"{APP_ID}.theme.aurora", compatibility={"skeleton": ">=0.5.0"})
        assert theme_codes(doctor(source(tmp_path / "range", manifest, theme=theme))) == [
            ("aurora", "theme_compatibility")
        ]
    with subtests.test(case="missing directory"):
        root = source(tmp_path / "missing", manifest)
        (root / "themes" / "aurora" / "theme.json").unlink()
        assert theme_codes(doctor(root)) == [("aurora", "missing_manifest")]


@pytest.mark.posix
def test_build_refuses_an_invalid_theme_and_packs_a_valid_one(tmp_path, manifest):
    from skeleton.system.packages import build_package, initialize_keys, validate_package

    private, public = initialize_keys(tmp_path / "private.pem", tmp_path / "public.pem")
    root = source(tmp_path / "good", manifest)
    package = build_package(root, root / "manifest.json", tmp_path / "good.skapp", private)
    members = validate_package(package, {"test-publisher": public}).manifest["files"]
    assert any(item["path"].endswith("themes/aurora/theme.json") for item in members)
    bad = source(tmp_path / "bad", manifest, files({"theme.css": b"@import 'x';"}))
    with pytest.raises(PackageError, match="Instance theme aurora is invalid"):
        build_package(bad, bad / "manifest.json", tmp_path / "bad.skapp", private)


def test_the_theme_checks_of_the_build_need_no_django(tmp_path, manifest):
    root = source(tmp_path, manifest)
    environment = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    code = (
        "import json, sys; from skeleton.system.packaging.themes import instance_theme_problems; "
        f"m = json.load(open({str(root / 'manifest.json')!r}, encoding='utf-8')); "
        f"print(instance_theme_problems({str(root)!r}, m), 'django' in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=environment, check=True)
    assert result.stdout.strip() == "[] False"
