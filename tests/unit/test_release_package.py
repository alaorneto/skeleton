"""The release package (#492): one signed .skapp with the core package, per-target locks and wheels."""

import hashlib
import json
import zipfile

import pytest

from skeleton.system import packages
from skeleton.system.packages import (
    PackageError,
    build_release,
    canonical_json,
    inspect_package,
    inspect_release,
    match_release_target,
    package_kind,
    read_release_member,
    run_command,
    validate_package,
    validate_release,
    validate_release_manifest,
)
from skeleton.system.packaging import limits
from tests.unit.release_helpers import (
    LINUX,
    PUBLISHER,
    build_test_release,
    core_package,
    inventory_item,
    lock_for,
    make_wheel,
    manifest_of,
    private_key,
    public_pem,
    read_archive,
    release_inputs,
    write_archive,
)

pytestmark = pytest.mark.posix


@pytest.fixture
def release(tmp_path):
    return build_test_release(tmp_path)


def test_build_then_validate_returns_verified_release(release):
    verified = validate_release(release["release"], release["trust"])
    assert verified.core_manifest["kind"] == "core"
    assert verified.manifest["version"] == verified.core_manifest["version"] == "0.6.0"
    assert verified.digest == hashlib.sha256(release["release"].read_bytes()).hexdigest()
    assert verified.core_digest == hashlib.sha256(release["core"].read_bytes()).hexdigest()
    assert verified.manifest["agent"]["wheel"] == "agent/skeleton_deployment_agent-0.6.0-py3-none-any.whl"
    target = match_release_target(verified.manifest, {**LINUX, "python_full_version": "3.14.1"})
    assert target["id"] == "cp314-manylinux_2_28_x86_64"
    assert match_release_target(verified.manifest, {**LINUX, "platform_machine": "riscv64"}) is None
    assert read_release_member(verified, target["lock"]).startswith(b"asgiref==3.12.1")


def test_shared_pure_wheel_is_stored_once_and_listed_in_both_targets(tmp_path):
    built = build_test_release(tmp_path, two_targets=True)
    names = zipfile.ZipFile(built["release"]).namelist()
    assert names.count("wheels/asgiref-3.12.1-py3-none-any.whl") == 1
    manifest = validate_release(built["release"], built["trust"]).manifest
    assert all("wheels/asgiref-3.12.1-py3-none-any.whl" in target["wheels"] for target in manifest["targets"])
    assert len(manifest["targets"]) == 2


def test_build_is_deterministic(tmp_path):
    inputs = release_inputs(tmp_path)
    first, second = (
        build_release(
            inputs["core"], tmp_path / name, inputs["private"], targets=inputs["targets"], agent_wheel=inputs["agent"]
        )
        for name in ("first.skapp", "second.skapp")
    )
    assert first.read_bytes() == second.read_bytes()


def test_build_never_overwrites(release):
    before = release["release"].read_bytes()
    with pytest.raises(PackageError, match="immutable"):
        build_release(release["core"], release["release"], release["private"], targets=release["targets"])
    assert release["release"].read_bytes() == before


def test_build_refuses_core_signed_by_another_key_before_writing(tmp_path):
    inputs = release_inputs(tmp_path)
    other, _ = private_key(tmp_path / "other.pem")
    output = tmp_path / "out.skapp"
    with pytest.raises(PackageError, match="same publisher key"):
        build_release(inputs["core"], output, other, targets=inputs["targets"])
    assert not output.exists()


def test_build_refuses_two_files_with_the_same_name(tmp_path):
    inputs = release_inputs(tmp_path)
    clash = make_wheel(tmp_path / "clash", "asgiref", "3.12.1", files={"asgiref/other.py": "X = 1\n"})
    inputs["targets"][0]["wheels"].append(clash)
    with pytest.raises(PackageError, match="share a name"):
        build_release(inputs["core"], tmp_path / "out.skapp", inputs["private"], targets=inputs["targets"])


def test_component_validator_refuses_a_release_package(release):
    with pytest.raises(PackageError):
        validate_package(release["release"], release["trust"])


def test_package_kind_of_each_type(tmp_path, release):
    assert package_kind(release["release"]) == "release"
    assert package_kind(release["core"]) == "core"
    app_manifest = {"kind": "app"}
    app = write_archive(tmp_path / "app.skapp", {"manifest.json": canonical_json(app_manifest)})
    assert package_kind(app) == "app"
    empty = write_archive(tmp_path / "empty.skapp", {"other.txt": b"x"})
    with pytest.raises(PackageError):
        package_kind(empty)
    unknown = write_archive(tmp_path / "unknown.skapp", {"manifest.json": canonical_json({"kind": "theme"})})
    with pytest.raises(PackageError, match="Unknown package kind"):
        package_kind(unknown)
    (tmp_path / "garbage.skapp").write_bytes(b"not a zip")
    with pytest.raises(PackageError):
        package_kind(tmp_path / "garbage.skapp")


def _resigned(release, tmp_path, change, *, members=None, private=None):
    """The release with its manifest changed by ``change`` and signed again."""
    current = read_archive(release["release"])
    manifest = json.loads(current["manifest.json"])
    change(manifest)
    contents = members(current) if members else current
    return write_archive(tmp_path / "changed.skapp", contents, manifest=manifest, private=private or release["private"])


def _member_change(release, tmp_path, name, data, *, role):
    """Replace one member and its inventory row consistently, re-signed: only deeper checks can fail."""

    def change(manifest):
        manifest["files"] = [
            inventory_item(name, data, role) if item["path"] == name else item for item in manifest["files"]
        ]

    def members(current):
        return {**current, name: data}

    return _resigned(release, tmp_path, change, members=members)


def test_tampered_archives_are_refused(release, tmp_path):
    contents = read_archive(release["release"])
    lock = "locks/cp314-manylinux_2_28_x86_64.txt"
    altered = write_archive(tmp_path / "altered.skapp", {**contents, lock: contents[lock] + b"# extra\n"})
    extra = write_archive(tmp_path / "extra.skapp", {**contents, "wheels/evil-1.0-py3-none-any.whl": b"x"})
    missing = write_archive(tmp_path / "missing.skapp", {k: v for k, v in contents.items() if k != lock})
    manifest = json.loads(contents["manifest.json"])
    noncanonical = write_archive(
        tmp_path / "noncanonical.skapp",
        contents,
        raw=json.dumps(manifest, indent=1).encode(),
        private=release["private"],
    )
    other, _ = private_key(tmp_path / "other.pem")
    foreign = write_archive(tmp_path / "foreign.skapp", contents, manifest=manifest, private=other)
    for path in (altered, extra, missing, noncanonical, foreign):
        with pytest.raises(PackageError):
            validate_release(path, release["trust"])
    with pytest.raises(PackageError, match="not trusted"):
        validate_release(release["release"], {"someone-else": release["public"]})
    with pytest.raises(PackageError, match="Trusted publisher"):
        validate_release(release["release"], None)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda m: m.update(unknown=1), id="unknown-field"),
        pytest.param(lambda m: m.update(release_format=2), id="format-2"),
        pytest.param(lambda m: m.update(kind="core"), id="kind"),
        pytest.param(lambda m: m["targets"][0].update(id="Bad Id"), id="invalid-target-id"),
        pytest.param(lambda m: m["targets"].append(dict(m["targets"][0])), id="duplicate-target"),
        pytest.param(lambda m: m["targets"][0]["environment"].pop("sys_platform"), id="environment-missing-key"),
        pytest.param(lambda m: m["targets"][0]["environment"].update(os_name="posix"), id="environment-extra-key"),
        pytest.param(lambda m: m["files"][0].update(path="packages/../x.skapp"), id="dot-dot-path"),
        pytest.param(lambda m: m["agent"].update(version="0.6.1"), id="agent-version"),
        pytest.param(lambda m: m.update(version="0.6.1"), id="release-version-vs-agent"),
        pytest.param(lambda m: m.update(version="v0.6.0"), id="unnormalized-version"),
    ],
)
def test_invalid_manifests_are_refused(release, tmp_path, change):
    with pytest.raises(PackageError):
        validate_release(_resigned(release, tmp_path, change), release["trust"])


def test_wheel_rules_inside_targets(release, tmp_path):
    manifest = validate_release(release["release"], release["trust"]).manifest
    target = manifest["targets"][0]
    native = next(path for path in target["wheels"] if "psycopg" in path)

    def duplicate_distribution(m):
        m["files"].append(inventory_item("wheels/asgiref-3.13.0-py3-none-any.whl", b"x", "wheel"))
        m["targets"][0]["wheels"].append("wheels/asgiref-3.13.0-py3-none-any.whl")

    def orphan(m):
        m["files"].append(inventory_item("wheels/orphan-1.0-py3-none-any.whl", b"x", "wheel"))

    def named_core(m):
        m["files"].append(inventory_item("wheels/django_skeleton-0.6.0-py3-none-any.whl", b"x", "wheel"))
        m["targets"][0]["wheels"].append("wheels/django_skeleton-0.6.0-py3-none-any.whl")

    for change in (duplicate_distribution, orphan, named_core):
        with pytest.raises(PackageError):
            validate_release_manifest(_copy_changed(manifest, change))
    unlocked = _member_change(release, tmp_path, native, b"different wheel bytes", role="wheel")
    with pytest.raises(PackageError, match="not pinned by its lock"):
        validate_release(unlocked, release["trust"])


def _copy_changed(manifest, change):
    value = json.loads(canonical_json(manifest))
    change(value)
    return value


def test_agent_wheel_of_another_distribution_is_refused(release, tmp_path):
    manifest = validate_release(release["release"], release["trust"]).manifest

    def renamed(m):
        old = m["agent"]["wheel"]
        m["agent"]["wheel"] = "agent/other_agent-0.6.0-py3-none-any.whl"
        for item in m["files"]:
            if item["path"] == old:
                item["path"] = m["agent"]["wheel"]

    with pytest.raises(PackageError, match="agent distribution"):
        validate_release_manifest(_copy_changed(manifest, renamed))

    def undeclared(m):
        m.pop("agent")

    with pytest.raises(PackageError, match="not declared"):
        validate_release_manifest(_copy_changed(manifest, undeclared))


def test_core_of_another_publisher_or_version_is_refused(tmp_path):
    inputs = release_inputs(tmp_path, agent=False)
    built = build_release(inputs["core"], tmp_path / "base.skapp", inputs["private"], targets=inputs["targets"])
    contents = read_archive(built)
    manifest = json.loads(contents["manifest.json"])
    for publisher, version in (("other-publisher", "0.6.0"), (PUBLISHER, "0.6.1")):
        directory = tmp_path / f"{publisher}-{version}"
        directory.mkdir()
        other = core_package(directory, inputs["private"], version, publisher=publisher, name="django-skeleton.skapp")
        data = other.read_bytes()
        changed = json.loads(canonical_json(manifest))
        old = changed["core"]["path"]
        changed["core"] = {"path": old, "sha256": hashlib.sha256(data).hexdigest()}
        changed["files"] = [inventory_item(old, data, "package") if i["path"] == old else i for i in changed["files"]]
        path = write_archive(
            directory / "release.skapp", {**contents, old: data}, manifest=changed, private=inputs["private"]
        )
        trust = {PUBLISHER: inputs["public"], "other-publisher": inputs["public"]}
        with pytest.raises(PackageError):
            validate_release(path, trust)


def test_target_limit_and_lock_size_limit(release, tmp_path, monkeypatch):
    manifest = validate_release(release["release"], release["trust"]).manifest
    monkeypatch.setattr(limits, "MAX_RELEASE_TARGETS", 0)
    with pytest.raises(PackageError, match="Invalid release targets"):
        validate_release_manifest(_copy_changed(manifest, lambda m: None))
    monkeypatch.undo()
    monkeypatch.setattr(limits, "MAX_METADATA_BYTES", 64)
    with pytest.raises(PackageError):
        validate_release(release["release"], release["trust"])


def test_too_many_targets_are_refused(release):
    manifest = validate_release(release["release"], release["trust"]).manifest

    def many(m):
        template = m["targets"][0]
        m["targets"] = [
            {**template, "id": f"t{index}", "environment": {**template["environment"], "python_version": f"3.{index}"}}
            for index in range(limits.MAX_RELEASE_TARGETS + 1)
        ]

    with pytest.raises(PackageError, match="Invalid release targets"):
        validate_release_manifest(_copy_changed(manifest, many))


def test_read_release_member_uses_the_snapshot(release):
    verified = validate_release(release["release"], release["trust"])
    release["release"].write_bytes(b"replaced after verification")
    data = read_release_member(verified, verified.manifest["core"]["path"])
    assert hashlib.sha256(data).hexdigest() == verified.core_digest
    with pytest.raises(PackageError, match="not in the signed inventory"):
        read_release_member(verified, "manifest.json")


def test_inspect_without_trust_reports_untrusted(release):
    result = inspect_release(release["release"])
    assert result["trusted"] is False
    assert result["core"]["manifest"]["kind"] == "core"
    assert result["manifest"]["kind"] == "release"
    trusted = inspect_release(release["release"], release["trust"])
    assert trusted["trusted"] is True and trusted["digest"] == result["digest"]


def test_cli_release_build_inspect_and_validate(tmp_path, capsys):
    inputs = release_inputs(tmp_path)
    wheels = tmp_path / "recipe-wheels"
    wheels.mkdir()
    for wheel in inputs["targets"][0]["wheels"]:
        (wheels / wheel.name).write_bytes(wheel.read_bytes())
    lock_for(tmp_path / "recipe.txt", sorted(wheels.glob("*.whl")))
    recipe = tmp_path / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "name": "Skeleton",
                "release_notes": "Notas.",
                "targets": [
                    {
                        "id": "cp314-manylinux_2_28_x86_64",
                        "environment": LINUX,
                        "lock": "recipe.txt",
                        "wheels": "recipe-wheels",
                    }
                ],
            }
        )
    )
    output = tmp_path / "cli-release.skapp"
    packages.main(
        [
            "release-build",
            "--core",
            str(inputs["core"]),
            "--recipe",
            str(recipe),
            "--output",
            str(output),
            "--private-key",
            str(inputs["private"]),
            "--agent-wheel",
            str(inputs["agent"]),
        ]
    )
    assert json.loads(capsys.readouterr().out) == {"artifact": str(output)}
    assert manifest_of(output)["release_notes"] == "Notas."
    store = tmp_path / "trust.json"
    store.write_text(json.dumps({PUBLISHER: str(public_pem(inputs["public"], tmp_path / "public.pem"))}))
    packages.main(["inspect", str(output)])
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["trusted"] is False and inspected["manifest"]["kind"] == "release"
    packages.main(["validate", str(output), "--trust-store", str(store)])
    validated = json.loads(capsys.readouterr().out)
    assert validated["trusted"] is True and validated["core"]["manifest"]["kind"] == "core"


def test_cli_inspect_of_component_packages_is_unchanged(tmp_path):
    private, public = private_key(tmp_path / "publisher.pem")
    core = core_package(tmp_path, private)
    store = tmp_path / "trust.json"
    store.write_text(json.dumps({PUBLISHER: str(public_pem(public, tmp_path / "public.pem"))}))
    assert run_command({"operation": "inspect", "path": str(core), "trust_store": None}) == inspect_package(core)
    trusted = run_command({"operation": "validate", "path": str(core), "trust_store": str(store)})
    assert trusted == inspect_package(core, {PUBLISHER: public})
