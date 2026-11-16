"""The release assets tool and its workflow (#496): no network, minimal wheels in tmp_path."""

import hashlib
import json
import re
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

from skeleton.system.packages import load_trust_store, validate_catalog, validate_package, validate_release
from tests.unit.release_helpers import LINUX, core_wheel, lock_for, make_wheel, private_key
from tools import release_artifacts

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release-artifacts.yml"
ASSETS = {
    "django-skeleton-0.6.0.skapp",
    "skeleton-0.6.0.release.skapp",
    "django_skeleton-0.6.0-py3-none-any.whl",
    "skeleton_deployment_agent-0.6.0-py3-none-any.whl",
    "skeleton-index.json",
    "SHA256SUMS",
}


@pytest.fixture
def inputs(tmp_path):
    wheels = tmp_path / "deps"
    dependencies = [
        make_wheel(wheels, "Django", "6.1.3", files={"django/__init__.py": ""}),
        make_wheel(wheels, "asgiref", "3.12.1"),
    ]
    lock = lock_for(tmp_path / "requirements.txt", dependencies)
    recipe = tmp_path / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "name": "Skeleton",
                "earliest_source": "0.5.0",
                "index_validity_days": 30,
                "targets": [{"id": "cp314-linux-x86_64", "environment": LINUX, "requirements": str(lock)}],
            }
        )
    )
    return {"recipe": recipe, "wheels": wheels, "tmp": tmp_path}


def wheels_for(inputs, version):
    directory = inputs["tmp"] / f"project-{version}"
    return (
        core_wheel(directory, version),
        make_wheel(directory, "skeleton-deployment-agent", version),
    )


def build(inputs, version="0.6.0", *, out=None, extra=(), key=None, publisher="skeleton"):
    core, agent = wheels_for(inputs, version)
    arguments = [
        "build",
        "--version",
        version,
        "--tag",
        f"v{version}",
        "--out",
        str(out or inputs["tmp"] / f"out-{version}"),
    ]
    arguments += ["--recipe", str(inputs["recipe"]), "--core-wheel", str(core), "--agent-wheel", str(agent)]
    arguments += ["--wheels-dir", str(inputs["wheels"]), *extra]
    if key is None:
        arguments.append("--ephemeral-key")
    else:
        arguments += ["--private-key", str(key), "--publisher", publisher]
    return release_artifacts.main(arguments)


pytestmark = pytest.mark.posix


def test_rehearsal_builds_the_six_assets_and_they_verify(inputs, capsys):
    assert build(inputs) == 0
    result = json.loads(capsys.readouterr().out)
    out = inputs["tmp"] / "out-0.6.0"
    assert {path.name for path in out.iterdir()} == ASSETS | {"trust.json"}
    trust = load_trust_store(out / "trust.json")
    validate_package(out / "django-skeleton-0.6.0.skapp", trust)
    release = validate_release(out / "skeleton-0.6.0.release.skapp", trust)
    assert release.manifest["agent"]["version"] == "0.6.0"
    index = validate_catalog(out / "skeleton-index.json", trust)
    (entry,) = index["entries"]
    assert entry["component_id"] == "django-skeleton" and entry["artifact"] == "v0.6.0/skeleton-0.6.0.release.skapp"
    assert entry["digest"] == hashlib.sha256((out / "skeleton-0.6.0.release.skapp").read_bytes()).hexdigest()
    sums = dict(line.split("  ")[::-1] for line in (out / "SHA256SUMS").read_text().splitlines())
    assert set(sums) == ASSETS - {"SHA256SUMS"}
    assert sums["skeleton-index.json"] == hashlib.sha256((out / "skeleton-index.json").read_bytes()).hexdigest()
    assert result["release"].endswith("skeleton-0.6.0.release.skapp")
    wheel = out / "django_skeleton-0.6.0-py3-none-any.whl"
    assert result["core_wheel"] == str(wheel)
    assert sums[wheel.name] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert "PRIVATE KEY" not in json.dumps(result)


def test_previous_index_accumulates_and_follows_the_line_policy(inputs, capsys):
    key, _ = private_key(inputs["tmp"] / "release.key")
    assert build(inputs, "0.6.0", key=key) == 0
    first = inputs["tmp"] / "out-0.6.0" / "skeleton-index.json"
    capsys.readouterr()
    assert build(inputs, "0.7.0", key=key, extra=("--previous-index", str(first))) == 0
    out = inputs["tmp"] / "out-0.7.0"
    trust = {"skeleton": release_artifacts._public_pem(key)}
    previous, current = validate_catalog(first, trust), validate_catalog(out / "skeleton-index.json", trust)
    assert [e["version"] for e in current["entries"]] == ["0.6.0", "0.7.0"]
    assert current["generation"] > previous["generation"]
    core = validate_package(out / "django-skeleton-0.7.0.skapp", trust).manifest
    assert SpecifierSet(core["migrations"]["from_versions"]) == SpecifierSet(">=0.6.0,<=0.7.0")


def test_previous_index_of_another_publisher_is_refused(inputs, capsys):
    key, _ = private_key(inputs["tmp"] / "release.key")
    assert build(inputs, "0.6.0", key=key) == 0
    first = inputs["tmp"] / "out-0.6.0" / "skeleton-index.json"
    other, _ = private_key(inputs["tmp"] / "other.key")
    with pytest.raises(SystemExit) as raised:
        build(inputs, "0.7.0", key=other, extra=("--previous-index", str(first)))
    assert raised.value.code == 1
    assert not any((inputs["tmp"] / "out-0.7.0").iterdir())


def test_wheel_version_must_be_the_release_version(inputs, capsys):
    core, agent = wheels_for(inputs, "0.6.1")
    out = inputs["tmp"] / "mismatch"
    arguments = ["build", "--ephemeral-key", "--version", "0.6.0", "--tag", "v0.6.0", "--out", str(out)]
    arguments += ["--recipe", str(inputs["recipe"]), "--core-wheel", str(core), "--agent-wheel", str(agent)]
    with pytest.raises(SystemExit) as raised:
        release_artifacts.main([*arguments, "--wheels-dir", str(inputs["wheels"])])
    assert raised.value.code == 1 and "version differs" in capsys.readouterr().err
    assert {path.name for path in out.iterdir()} <= {"trust.json"}


@pytest.mark.parametrize(
    "arguments,message",
    [
        (["--tag", "0.6.0"], "Invalid release tag"),
        (["--tag", "v0.6.0;rm -rf /"], "Invalid release tag"),
        (["--tag", "v0.6.1"], "does not name --version"),
        (["--tag", "v0.6.0", "--private-key", "x.pem"], "does not combine"),
    ],
)
def test_invalid_invocations_are_refused(inputs, capsys, arguments, message):
    with pytest.raises(SystemExit) as raised:
        release_artifacts.main(
            ["build", "--ephemeral-key", "--version", "0.6.0", "--out", str(inputs["tmp"] / "o"), *arguments]
        )
    assert raised.value.code == 1 and message in capsys.readouterr().err


def test_output_directory_must_be_new_or_empty(inputs, capsys):
    out = inputs["tmp"] / "busy"
    out.mkdir()
    (out / "old.txt").write_text("x")
    with pytest.raises(SystemExit):
        build(inputs, out=out)
    assert "--out must not exist" in capsys.readouterr().err


def test_refresh_index_keeps_entries_and_renews_generation(inputs, capsys):
    key, _ = private_key(inputs["tmp"] / "release.key")
    assert build(inputs, "0.6.0", key=key) == 0
    first = inputs["tmp"] / "out-0.6.0" / "skeleton-index.json"
    capsys.readouterr()
    out = inputs["tmp"] / "refreshed"
    arguments = ["refresh-index", "--publisher", "skeleton", "--private-key", str(key), "--previous-index", str(first)]
    assert release_artifacts.main([*arguments, "--out", str(out), "--recipe", str(inputs["recipe"])]) == 0
    trust = {"skeleton": release_artifacts._public_pem(key)}
    old, new = validate_catalog(first, trust), validate_catalog(out / "skeleton-index.json", trust)
    assert new["entries"] == old["entries"] and new["generation"] > old["generation"]
    assert new["expires_at"] >= old["expires_at"]


def test_verify_install_needs_a_matching_interpreter(inputs, capsys, monkeypatch):
    monkeypatch.setattr(release_artifacts, "default_environment", lambda: {**LINUX, "platform_machine": "riscv64"})
    with pytest.raises(SystemExit):
        build(inputs, extra=("--verify-install",))
    assert "matches a recipe target" in capsys.readouterr().err


def test_versioned_recipe_names_the_official_target():
    recipe = json.loads((ROOT / "deploy" / "release" / "recipe.json").read_text(encoding="utf-8"))
    (target,) = recipe["targets"]
    assert target["requirements"] == "requirements.txt" and (ROOT / target["requirements"]).is_file()
    assert target["environment"] == LINUX and recipe["earliest_source"] == "0.5.0"


def run_blocks(text):
    """The text of every run: block of a workflow (folded lines included)."""
    blocks, current, indent = [], None, 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if current is not None:
            if stripped and len(line) - len(stripped) <= indent:
                blocks.append("\n".join(current))
                current = None
            else:
                current.append(line)
                continue
        match = re.match(r"(\s*)(?:- )?run:\s*(.*)$", line)
        if match:
            indent, current = len(match.group(1)), [match.group(2)]
    if current is not None:
        blocks.append("\n".join(current))
    return blocks


def test_workflow_keeps_least_privilege_and_no_input_interpolation():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^permissions: \{\}$", text, re.M)
    assert text.count("contents: write") == 2  # publish and refresh only
    triggers = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "pull_request" not in triggers and {"workflow_dispatch:", "schedule:"} <= set(triggers.split())
    blocks = run_blocks(text)
    assert blocks and not [block for block in blocks if "${{" in block]
    assert "persist-credentials: false" in text and 'rm -f "$RUNNER_TEMP/release.key"' in text
    assert text.count("if: always()") == 2


def test_the_core_wheel_is_attached_with_its_hash_in_the_release_body():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '"$RUNNER_TEMP"/out/*.whl' in text
    (block,) = [block for block in run_blocks(text) if "gh release edit" in block]
    assert "django_skeleton-" in block and "SHA256SUMS" in block and "--notes-file" in block


def test_release_dispatches_the_artifacts_workflow():
    configuration = (ROOT / ".github" / "dev-workflow.toml").read_text(encoding="utf-8")
    assert 'dispatch_workflow = "release-artifacts.yml"' in configuration
    assert "actions: write" in (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def installable(inputs, *, agent_requires):
    """A recipe for this interpreter and an agent wheel with a launcher, for --verify-install."""
    from packaging.markers import default_environment

    environment = {key: default_environment()[key] for key in LINUX}
    recipe = json.loads(inputs["recipe"].read_text())
    recipe["targets"][0]["environment"] = environment
    inputs["recipe"].write_text(json.dumps(recipe))
    directory = inputs["tmp"] / "installable"
    core = core_wheel(directory, "0.6.0")
    agent = make_wheel(
        directory,
        "skeleton-deployment-agent",
        "0.6.0",
        files={
            "deployment_agent/__init__.py": "",
            "deployment_agent/__main__.py": "def main():\n    print('usage')\n",
            "skeleton_deployment_agent-0.6.0.dist-info/entry_points.txt": (
                "[console_scripts]\nskeleton-deployment-agent = deployment_agent.__main__:main\n"
            ),
        },
        requires=agent_requires,
    )
    return core, agent


def verify(inputs, core, agent, name):
    arguments = ["build", "--ephemeral-key", "--version", "0.6.0", "--tag", "v0.6.0", "--verify-install"]
    arguments += ["--out", str(inputs["tmp"] / name), "--recipe", str(inputs["recipe"]), "--core-wheel", str(core)]
    return release_artifacts.main([*arguments, "--agent-wheel", str(agent), "--wheels-dir", str(inputs["wheels"])])


def test_verify_install_installs_offline_and_refuses_a_diverging_agent_pin(inputs, capsys):
    core, agent = installable(inputs, agent_requires=("django-skeleton==0.6.0", "asgiref==3.12.1"))
    assert verify(inputs, core, agent, "installed") == 0
    capsys.readouterr()
    core, agent = installable(inputs, agent_requires=("django-skeleton==0.6.0", "asgiref==9.9.9"))
    with pytest.raises(SystemExit) as raised:
        verify(inputs, core, agent, "diverging")
    assert raised.value.code == 1 and "command failed" in capsys.readouterr().err
