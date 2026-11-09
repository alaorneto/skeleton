"""Regeneração e validação dos locks dos PRs do Dependabot (tools/workflow/dependabot_locks.py)."""

from pathlib import Path

import pytest

from tools.workflow.dependabot_locks import (
    HEADER_FIRST_LINE,
    check_artifact,
    in_requirements,
    lock_problems,
    lock_versions,
    main,
    official_command,
    upgrade_specs,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def lock(name: str, *blocks: str) -> str:
    source = name.replace(".txt", ".in")
    header = f"{HEADER_FIRST_LINE}\n#    {official_command(source, name)}\n"
    return header + "".join(blocks)


def pin(package: str, version: str, *hashes: str) -> str:
    lines = [f"{package}=={version}"] + [f"    --hash=sha256:{value}" for value in hashes or (HASH_A,)]
    return " \\\n".join(lines) + "\n    # via -r requirements.in\n"


def test_official_command_matches_the_readme_and_ci():
    readme = Path("README.md").read_text(encoding="utf-8")
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    for source, output in (("requirements.in", "requirements.txt"), ("requirements-dev.in", "requirements-dev.txt")):
        command = official_command(source, output)
        assert command in readme
        assert command in ci


def test_lock_versions_ignore_extras_and_normalize_names():
    text = "psycopg[binary]==3.3.8 \\\n    --hash=sha256:x\nDjango_Allauth==65.21.1 \\\n    # via x\n"
    assert lock_versions(text) == {"psycopg": "3.3.8", "django-allauth": "65.21.1"}


def test_in_requirements_skip_options_and_comments():
    text = "-r requirements.in\n# comentário\nmypy == 2.4.1  # tipos\nDjango_Stubs[compatible-mypy]==6.1.2\n\n"
    assert in_requirements(text) == {"mypy": "mypy==2.4.1", "django-stubs": "django_stubs[compatible-mypy]==6.1.2"}


def test_upgrade_specs_pin_versions_changed_in_the_locks():
    base = "mypy==1.19.1 \\\npip==26.2.1 \\\nsetuptools==84.2.1 \\\n"
    head = "mypy==2.4.1 \\\nast-serialize==0.13.1 \\\ncoverage[toml]==7.17.2 \\\n"
    base_in = "mypy==1.19.1\n"
    head_in = "mypy==2.4.1\n"
    specs = upgrade_specs([base + "coverage==7.17.2 \\\n"], [head], [base_in], [head_in])
    # pip e setuptools sumiram no estilo pip-compile: não viram atualização.
    assert specs == ["ast-serialize==0.13.1", "mypy==2.4.1"]


def test_upgrade_specs_take_names_from_in_files_when_the_lock_was_not_regenerated():
    locks = ["django==5.2.18 \\\n"]
    specs = upgrade_specs(locks, locks, ["Django==5.2.18\nredis>=5\n"], ["Django==6.1.3\nredis>=5\n"])
    assert specs == ["django"]


def test_upgrade_specs_merge_both_locks():
    base = ["django==5.2.18 \\\n", "django==5.2.18 \\\npytest==9.1.1 \\\n"]
    head = ["django==6.1.3 \\\n", "django==5.2.18 \\\npytest==9.2.0 \\\n"]
    # O lock de dev do PR ficou para trás no Django: a versão nova vale para os dois.
    assert upgrade_specs(base, head, ["", ""], ["", ""]) == ["django==6.1.3", "pytest==9.2.0"]


def test_upgrade_specs_are_empty_without_changes():
    locks = ["mypy==2.4.1 \\\n"]
    assert upgrade_specs(locks, locks, ["mypy==2.4.1\n"], ["mypy==2.4.1\n"]) == []


def test_lock_problems_accept_the_official_format():
    text = lock("requirements.txt", pin("django", "6.1.3", HASH_A, HASH_B), pin("whitenoise", "6.12.0"))
    assert lock_problems("requirements.txt", text) == []


@pytest.mark.parametrize(
    "block",
    [
        "--index-url https://example.invalid/simple\n",
        "--extra-index-url https://example.invalid/simple\n",
        "--find-links https://example.invalid/wheels\n",
        "--trusted-host example.invalid\n",
        "-e git+https://example.invalid/pkg.git#egg=pkg\n",
        "pkg @ https://example.invalid/pkg-1.0.tar.gz \\\n    --hash=sha256:" + HASH_A + "\n",
        "pkg>=1.0 \\\n    --hash=sha256:" + HASH_A + "\n",
        "pkg==1.0\n",
        "pkg==1.0 \\\n    --hash=md5:" + "a" * 32 + "\n",
        "pkg==1.0 ; python_version < '3.13' \\\n    --hash=sha256:" + HASH_A + "\n",
        "-r other.txt\n",
        "pkg==1.0 \\\n",
    ],
)
def test_lock_problems_refuse_anything_but_hashed_pins(block):
    text = lock("requirements.txt", pin("django", "6.1.3"), block)
    assert lock_problems("requirements.txt", text)


def test_lock_problems_refuse_wrong_header_name_line_endings_and_duplicates():
    good = lock("requirements.txt", pin("django", "6.1.3"))
    assert lock_problems("setup.py", good) == ["setup.py: arquivo inesperado"]
    assert lock_problems("requirements-dev.txt", good)  # cabeçalho de outro arquivo
    assert lock_problems("requirements.txt", good.replace("--generate-hashes ", ""))
    assert lock_problems("requirements.txt", good.replace("\n", "\r\n"))
    assert lock_problems("requirements.txt", good + pin("Django", "6.1.3"))
    assert lock_problems("requirements.txt", lock("requirements.txt"))


def write_artifact(directory: Path, **files: str) -> Path:
    directory.mkdir()
    for name, text in files.items():
        (directory / name).write_bytes(text.encode("utf-8"))
    return directory


def valid_files() -> dict[str, str]:
    return {
        "requirements.txt": lock("requirements.txt", pin("django", "6.1.3")),
        "requirements-dev.txt": lock("requirements-dev.txt", pin("django", "6.1.3"), pin("mypy", "2.4.1")),
    }


def test_check_artifact_accepts_both_official_locks(tmp_path, capsys):
    assert check_artifact(write_artifact(tmp_path / "artifact", **valid_files())) == 0
    assert "válidos" in capsys.readouterr().out


def test_check_artifact_requires_exactly_the_two_locks(tmp_path, capsys):
    files = valid_files()
    missing = write_artifact(tmp_path / "missing", **{"requirements.txt": files["requirements.txt"]})
    assert check_artifact(missing) == 1
    extra = write_artifact(tmp_path / "extra", **files, **{"setup.py": "print('x')\n"})
    assert check_artifact(extra) == 1
    assert check_artifact(tmp_path / "absent") == 1
    assert capsys.readouterr().out.count("::error::") >= 3


def test_check_artifact_refuses_tampered_lock(tmp_path):
    files = valid_files()
    files["requirements-dev.txt"] += "--extra-index-url https://example.invalid/simple\n"
    assert check_artifact(write_artifact(tmp_path / "artifact", **files)) == 1


def test_check_artifact_refuses_directories_and_non_utf8(tmp_path):
    files = valid_files()
    directory = write_artifact(tmp_path / "artifact", **{"requirements.txt": files["requirements.txt"]})
    (directory / "requirements-dev.txt").mkdir()
    assert check_artifact(directory) == 1
    binary = tmp_path / "binary"
    binary.mkdir()
    (binary / "requirements.txt").write_bytes(b"\xff\xfe")
    (binary / "requirements-dev.txt").write_text(files["requirements-dev.txt"], encoding="utf-8")
    assert check_artifact(binary) == 1


def test_main_refuses_unknown_arguments_and_non_sha_base(capsys):
    assert main(["regenerate"]) == 2
    assert main(["regenerate", "--base", "main; rm -rf /"]) == 2
    assert main(["outro"]) == 2
    assert "uso:" in capsys.readouterr().err
