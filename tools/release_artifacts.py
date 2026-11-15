"""Build, sign and verify the release assets of a core version (#496).

``build`` produces, in a new directory, the five assets every GitHub release carries:

- ``django-skeleton-<version>.skapp``: the core package (not an index entry);
- ``skeleton-<version>.release.skapp``: the release package, the core entry of the index;
- ``skeleton_deployment_agent-<version>-py3-none-any.whl``: the agent wheel, for manual installs;
- ``skeleton-index.json``: the signed, accumulated index (``artifact`` is ``<tag>/<asset>``);
- ``SHA256SUMS``: a convenience; trust comes from the signatures.

``refresh-index`` signs the same entries again with a new generation and validity. Only the
standard library and ``skeleton``; commands run as argument lists, never through a shell. The key
content is never printed.

    python tools/release_artifacts.py build --version 0.6.0 --tag v0.6.0 --publisher skeleton \\
        --private-key /private/release.key --out /tmp/out [--previous-index skeleton-index.json]
    python tools/release_artifacts.py build --ephemeral-key --version 0.6.0 --tag v0.6.0 --out /tmp/rehearsal
    python tools/release_artifacts.py refresh-index --publisher skeleton --private-key /private/release.key \\
        --previous-index skeleton-index.json --out /tmp/out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # Run as a script from any directory.
    sys.path.insert(0, str(ROOT))

from packaging.markers import default_environment  # noqa: E402
from packaging.utils import canonicalize_name, parse_wheel_filename  # noqa: E402
from packaging.version import Version  # noqa: E402

from skeleton.distribution import build_core_release, core_manifest, wheel_inventory  # noqa: E402
from skeleton.system.packages import (  # noqa: E402
    AGENT_DISTRIBUTION,
    TARGET_ENVIRONMENT_KEYS,
    PackageError,
    _private_key,
    build_release,
    initialize_keys,
    run_command,
    validate_catalog,
    validate_package,
    validate_release,
)
from skeleton.versioning import normalize_version  # noqa: E402

TAG = re.compile(r"v\d+\.\d+\.\d+([a-z0-9.+-]*)\Z")
RECIPE = ROOT / "deploy" / "release" / "recipe.json"
INDEX = "skeleton-index.json"
# The default upload limit of the agent and of the platform page (max_upload_bytes, SKELETON_PACKAGE_MAX_BYTES).
MAX_RELEASE_BYTES = 128 * 1024 * 1024


class ToolError(Exception):
    pass


def _fail(message: str) -> None:
    raise ToolError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recipe(path: Path) -> dict:
    recipe = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(recipe, dict) or not isinstance(recipe.get("targets"), list) or not recipe["targets"]:
        _fail("Release recipe needs at least one target.")
    for target in recipe["targets"]:
        if not isinstance(target, dict) or not {"id", "environment", "requirements"} <= set(target):
            _fail("Each recipe target needs id, environment and requirements.")
    days = recipe.get("index_validity_days", 120)
    if type(days) is not int or not 1 <= days <= 365:
        _fail("index_validity_days must be an integer from 1 to 365.")
    return recipe


def _empty_output(path: Path) -> Path:
    path = Path(path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        _fail("--out must not exist or be an empty directory.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _public_pem(private_key: Path) -> str:
    from cryptography.hazmat.primitives import serialization

    return (
        _private_key(private_key)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )


def _trust_file(directory: Path, publisher: str, private_key: Path) -> Path:
    """A trust store with the public key derived from the signing key (never the private one)."""
    path = directory / "trust.json"
    path.write_text(json.dumps({publisher: _public_pem(private_key)}), encoding="utf-8")
    return path


def _previous(path: Path | None, trust: dict) -> dict | None:
    """The previous index, verified with the publisher's key (expiry aside)."""
    if path is None:
        return None
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = envelope.get("catalog", {}).get("entries", []) if isinstance(envelope, dict) else []
    channels = tuple({entry.get("channel") for entry in entries if isinstance(entry, dict)}) or ("stable",)
    return validate_catalog(envelope, trust, allow_expired=True, channels=channels)


def _build_wheels(work: Path) -> tuple[Path, Path]:
    """The core and agent wheels, built like the CI build job builds them."""
    for name, source in (("dist", ROOT), ("dist-agent", ROOT / "deployment_agent")):
        command = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir"]
        subprocess.run([*command, str(work / name), str(source)], check=True, cwd=work)
    return _single(work / "dist"), _single(work / "dist-agent")


def _single(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        _fail(f"Expected one wheel in {directory.name}.")
    return wheels[0]


def _check_versions(version: str, core_wheel: Path, agent_wheel: Path) -> None:
    if normalize_version(wheel_inventory(core_wheel)["version"]) != version:
        _fail("The core wheel version differs from --version.")
    name, agent_version, _, _ = parse_wheel_filename(agent_wheel.name)
    if canonicalize_name(name) != AGENT_DISTRIBUTION or agent_version != Version(version):
        _fail("The agent wheel version differs from --version.")


def _download(target: dict, lock: Path, destination: Path) -> None:
    """Only binary wheels, hash-checked against the versioned lock, for the target's platforms."""
    pip = target.get("pip") or {}
    command = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-deps", "--require-hashes"]
    command += ["-r", str(lock), "--dest", str(destination)]
    for option in ("python_version", "implementation", "abi"):
        if pip.get(option):
            command += ["--" + option.replace("_", "-"), str(pip[option])]
    for platform in pip.get("platforms", ()):
        command += ["--platform", str(platform)]
    subprocess.run(command, check=True)


def _targets(recipe: dict, work: Path, wheels_dir: Path | None) -> list[dict]:
    targets = []
    for target in recipe["targets"]:
        lock = ROOT / target["requirements"]
        if not lock.is_file():
            _fail(f"Lock of target {target['id']} is missing.")
        if wheels_dir is not None:
            directory = wheels_dir / target["id"] if (wheels_dir / target["id"]).is_dir() else wheels_dir
        else:
            directory = work / "wheels" / target["id"]
            directory.mkdir(parents=True)
            _download(target, lock, directory)
        copy = work / "locks" / f"{target['id']}.txt"
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(lock.read_bytes())  # A faithful copy of the recipe's lock.
        wheels = sorted(directory.glob("*.whl"), key=lambda path: path.name)
        targets.append({"id": target["id"], "environment": target["environment"], "lock": copy, "wheels": wheels})
    return targets


def _generation(previous: dict | None) -> int:
    generation = int(time.time())
    if previous is not None and previous["generation"] >= generation:
        generation = previous["generation"] + 1
    return generation


def _index(
    release: Path | None,
    output: Path,
    private_key: Path,
    trust: Path,
    publisher: str,
    *,
    tag: str = "",
    previous: Path | None,
    validity_days: int,
    generation: int,
) -> Path:
    run_command(
        {
            "operation": "index",
            "artifacts": [str(release)] if release else [],
            "output": str(output),
            "private_key": str(private_key),
            "trust_store": str(trust),
            "publisher": publisher,
            "expires_at": (datetime.now(UTC) + timedelta(days=validity_days)).isoformat(),
            "generation": generation,
            "channel": "stable",
            "artifact_base": tag,
            "previous": str(previous) if previous else None,
        }
    )
    return output


def _verify(out: Path, *, version: str, tag: str, publisher: str, trust: dict, release: Path, core: Path) -> None:
    validate_package(core, trust)
    validate_release(release, trust)
    body = validate_catalog(out / INDEX, trust, channels=("stable",))
    entries = [e for e in body["entries"] if e["component_id"] == "django-skeleton" and e["version"] == version]
    if (
        len(entries) != 1
        or entries[0]["digest"] != _sha256(release)
        or entries[0]["artifact"] != f"{tag}/{release.name}"
        or body["publisher"] != publisher
    ):
        _fail("The index does not describe the release package.")
    if release.stat().st_size > MAX_RELEASE_BYTES:
        _fail("Release package exceeds the default upload limit.")


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _verify_install(recipe: dict, targets: list[dict], core_wheel: Path, agent_wheel: Path, work: Path) -> None:
    """Install, offline, what the agent installs when it updates itself, and run pip check."""
    environment = default_environment()
    matching = [
        target
        for target in targets
        if all(environment.get(key) == target["environment"].get(key) for key in TARGET_ENVIRONMENT_KEYS)
    ]
    if not matching:
        _fail("--verify-install needs an interpreter that matches a recipe target.")
    target = matching[0]
    links = work / "verify-wheels"
    links.mkdir()
    for wheel in [*target["wheels"], core_wheel, agent_wheel]:
        shutil.copyfile(wheel, links / wheel.name)
    lock = work / "verify.lock"
    core_version = wheel_inventory(core_wheel)["version"]
    lines = [
        target["lock"].read_text(encoding="utf-8").rstrip("\n"),
        f"django-skeleton=={core_version} --hash=sha256:{_sha256(core_wheel)}",
        f"{AGENT_DISTRIBUTION}=={parse_wheel_filename(agent_wheel.name)[1]} --hash=sha256:{_sha256(agent_wheel)}",
    ]
    lock.write_text("\n".join(lines) + "\n", encoding="utf-8")
    venv = work / "verify-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = _venv_python(venv)
    install = [str(python), "-m", "pip", "install", "--no-index", "--only-binary=:all:", "--require-hashes"]
    subprocess.run([*install, "--find-links", str(links), "-r", str(lock)], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    launcher = python.parent / (
        "skeleton-deployment-agent.exe" if sys.platform == "win32" else "skeleton-deployment-agent"
    )
    subprocess.run([str(launcher), "--help"], check=True, stdout=subprocess.DEVNULL)


def build(options: argparse.Namespace) -> dict:
    if not TAG.fullmatch(options.tag or ""):
        _fail("Invalid release tag.")
    version = normalize_version(options.version)
    if normalize_version(options.tag[1:]) != version:
        _fail("The tag does not name --version.")
    if options.ephemeral_key and (options.private_key or options.publisher):
        _fail("--ephemeral-key does not combine with --private-key or --publisher.")
    if not options.ephemeral_key and not (options.private_key and options.publisher):
        _fail("--private-key and --publisher are required (or --ephemeral-key for a rehearsal).")
    recipe = _recipe(options.recipe)
    out = _empty_output(options.out)
    with tempfile.TemporaryDirectory(prefix="skeleton-release-") as temporary:
        work = Path(temporary)
        if options.ephemeral_key:
            private_key, _ = initialize_keys(work / "keys" / "private.pem", work / "keys" / "public.pem")
            publisher = "ephemeral"
            trust_path = _trust_file(out, publisher, private_key)
        else:
            private_key, publisher = Path(options.private_key), options.publisher
            trust_path = _trust_file(work, publisher, private_key)
        trust = {publisher: json.loads(trust_path.read_text(encoding="utf-8"))[publisher]}
        previous_path = Path(options.previous_index) if options.previous_index else None
        previous = _previous(previous_path, trust)
        if options.core_wheel and options.agent_wheel:
            core_wheel, agent_wheel = Path(options.core_wheel), Path(options.agent_wheel)
        elif options.core_wheel or options.agent_wheel:
            _fail("--core-wheel and --agent-wheel go together.")
        else:
            core_wheel, agent_wheel = _build_wheels(work)
        _check_versions(version, core_wheel, agent_wheel)
        targets = _targets(recipe, work, Path(options.wheels_dir) if options.wheels_dir else None)
        known = [e["version"] for e in (previous or {}).get("entries", []) if e["component_id"] == "django-skeleton"]
        manifest = core_manifest(
            core_wheel, publisher=publisher, known_versions=known, earliest_source=recipe.get("earliest_source")
        )
        manifest_path = work / "core-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        core = build_core_release(core_wheel, manifest_path, out / f"django-skeleton-{version}.skapp", private_key)
        release = build_release(
            core,
            out / f"skeleton-{version}.release.skapp",
            private_key,
            targets=targets,
            agent_wheel=agent_wheel,
            name=recipe.get("name", "Skeleton"),
        )
        _index(
            release,
            out / INDEX,
            private_key,
            trust_path,
            publisher,
            tag=options.tag,
            previous=previous_path,
            validity_days=recipe.get("index_validity_days", 120),
            generation=_generation(previous),
        )
        agent_copy = out / agent_wheel.name
        shutil.copyfile(agent_wheel, agent_copy)
        _verify(out, version=version, tag=options.tag, publisher=publisher, trust=trust, release=release, core=core)
        if options.verify_install:
            _verify_install(recipe, targets, core_wheel, agent_wheel, work)
    assets = [core, release, agent_copy, out / INDEX]
    sums = out / "SHA256SUMS"
    sums.write_text("".join(f"{_sha256(path)}  {path.name}\n" for path in assets), encoding="utf-8")
    result = {
        "core": str(core),
        "release": str(release),
        "agent": str(agent_copy),
        "index": str(out / INDEX),
        "sha256sums": str(sums),
    }
    if options.ephemeral_key:
        result["trust_store"] = str(out / "trust.json")
    return result


def refresh_index(options: argparse.Namespace) -> dict:
    recipe = _recipe(options.recipe)
    out = _empty_output(options.out)
    with tempfile.TemporaryDirectory(prefix="skeleton-index-") as temporary:
        trust_path = _trust_file(Path(temporary), options.publisher, Path(options.private_key))
        trust = {options.publisher: json.loads(trust_path.read_text(encoding="utf-8"))[options.publisher]}
        previous = _previous(Path(options.previous_index), trust)
        _index(
            None,
            out / INDEX,
            Path(options.private_key),
            trust_path,
            options.publisher,
            previous=Path(options.previous_index),
            validity_days=recipe.get("index_validity_days", 120),
            generation=_generation(previous),
        )
        channels = tuple({entry["channel"] for entry in previous["entries"]}) or ("stable",)
        validate_catalog(out / INDEX, trust, minimum_generation=previous["generation"] + 1, channels=channels)
    return {"index": str(out / INDEX)}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="release_artifacts", description=__doc__.split("\n\n")[0])
    commands = root.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build", help="Build, sign and verify the assets of one release.")
    make.add_argument("--version", required=True)
    make.add_argument("--tag", required=True)
    make.add_argument("--publisher")
    make.add_argument("--private-key")
    make.add_argument("--out", required=True)
    make.add_argument("--recipe", default=str(RECIPE))
    make.add_argument("--previous-index")
    make.add_argument("--ephemeral-key", action="store_true", help="Rehearsal with a throwaway key (CI).")
    make.add_argument("--core-wheel")
    make.add_argument("--agent-wheel")
    make.add_argument("--wheels-dir", help="Dependency wheels already at hand, instead of pip download.")
    make.add_argument("--verify-install", action="store_true")
    refresh = commands.add_parser("refresh-index", help="Sign the same index with a new generation and validity.")
    for name in ("publisher", "private-key", "previous-index", "out"):
        refresh.add_argument("--" + name, required=True)
    refresh.add_argument("--recipe", default=str(RECIPE))
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    options = arguments.parse_args(argv)
    try:
        result = build(options) if options.command == "build" else refresh_index(options)
    except (ToolError, PackageError, ValueError, OSError, KeyError) as error:
        arguments.exit(1, f"release_artifacts: {error}\n")
    except subprocess.CalledProcessError as error:
        arguments.exit(1, f"release_artifacts: command failed with exit code {error.returncode}.\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    os.umask(0o077)  # Keys and temporary files stay private to the runner user.
    raise SystemExit(main())
