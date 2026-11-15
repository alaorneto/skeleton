"""Builders of minimal wheels, signed core packages and release packages for the tests (#492).

Everything is written under the caller's ``tmp_path``; nothing is executed or downloaded.
"""

import hashlib
import io
import json
import zipfile
from base64 import b64encode
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.utils import canonicalize_name

from skeleton.system.packages import build_artifact, build_release, canonical_json

PUBLISHER = "fixture-publisher"
LINUX = {
    "implementation_name": "cpython",
    "python_version": "3.14",
    "platform_machine": "x86_64",
    "sys_platform": "linux",
}
ARM = {**LINUX, "platform_machine": "aarch64"}


def private_key(path):
    """A fresh Ed25519 private key in PEM at path; returns (path, public key)."""
    key = Ed25519PrivateKey.generate()
    path = Path(path)
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return path, key.public_key()


def public_pem(public_key, path):
    path = Path(path)
    path.write_bytes(
        public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return path


def make_wheel(directory, name, version, *, files=None, requires=(), python=None, tag="py3-none-any"):
    """A minimal valid wheel; ``files`` maps archive paths to text."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    distribution = canonicalize_name(name).replace("-", "_")
    path = directory / f"{distribution}-{version}-{tag}.whl"
    info = f"{distribution}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    if python:
        metadata += f"Requires-Python: {python}\n"
    metadata += "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
    with zipfile.ZipFile(path, "w") as archive:
        for member, text in (files or {f"{distribution}/__init__.py": ""}).items():
            archive.writestr(member, text)
        archive.writestr(f"{info}/METADATA", metadata)
        archive.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{info}/RECORD", "")
    return path


def core_wheel(directory, version="0.6.0", *, requires=("Django==6.1.3",), python=">=3.12,<3.15"):
    return make_wheel(
        directory,
        "django-skeleton",
        version,
        files={"skeleton/__init__.py": "", "skeleton/conf/base.py": "VALUE = 1\n"},
        requires=requires,
        python=python,
    )


def core_manifest(wheel, version="0.6.0", *, publisher=PUBLISHER, from_versions=">=0.5.0,<=0.6.0"):
    return {
        "manifest_version": 2,
        "kind": "core",
        "component_id": "django-skeleton",
        "name": "Skeleton",
        "description": "Test core release",
        "version": version,
        "publisher": publisher,
        "compatibility": {"skeleton": from_versions, "python": ">=3.12,<3.15", "django": ">=6.1,<6.2"},
        "dependencies": {},
        "migrations": {"leaves": [], "from_versions": from_versions},
        "files": [],
        "python_dependencies": [
            {"name": "django-skeleton", "version": version, "wheel": "wheels/" + Path(wheel).name, "sha256": "0" * 64}
        ],
        "lifecycle": {"data_retention": "retain", "maintenance": True, "backup": True},
    }


def core_package(directory, private, version="0.6.0", *, publisher=PUBLISHER, name=None, **manifest):
    directory = Path(directory)
    wheel = core_wheel(directory / f"core-wheel-{version}", version)
    output = directory / (name or f"django-skeleton-{version}.skapp")
    return build_artifact(
        core_manifest(wheel, version, publisher=publisher, **manifest),
        {"wheels/" + wheel.name: wheel},
        output,
        private,
    )


def lock_for(path, wheels):
    """A hash-locked requirements file pinning exactly ``wheels``."""
    lines = []
    for wheel in wheels:
        name, version = Path(wheel).name.split("-")[:2]
        lines.append(f"{name}=={version} --hash=sha256:{hashlib.sha256(Path(wheel).read_bytes()).hexdigest()}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)


def release_inputs(tmp_path, version="0.6.0", *, two_targets=False, agent=True):
    """A signed core package, its targets and the agent wheel, ready for build_release."""
    private, public = private_key(tmp_path / "publisher.pem")
    core = core_package(tmp_path, private, version)
    shared = make_wheel(tmp_path / "wheels", "asgiref", "3.12.1")
    native = make_wheel(tmp_path / "wheels-x86", "psycopg-binary", "3.3.0", tag="cp314-cp314-manylinux_2_28_x86_64")
    targets = [
        {
            "id": "cp314-manylinux_2_28_x86_64",
            "environment": dict(LINUX),
            "lock": lock_for(tmp_path / "x86.txt", [shared, native]),
            "wheels": [shared, native],
        }
    ]
    if two_targets:
        arm = make_wheel(tmp_path / "wheels-arm", "psycopg-binary", "3.3.0", tag="cp314-cp314-manylinux_2_28_aarch64")
        targets.append(
            {
                "id": "cp314-manylinux_2_28_aarch64",
                "environment": dict(ARM),
                "lock": lock_for(tmp_path / "arm.txt", [shared, arm]),
                "wheels": [shared, arm],
            }
        )
    agent_wheel = make_wheel(tmp_path / "agent", "skeleton-deployment-agent", version) if agent else None
    return {
        "private": private,
        "public": public,
        "core": core,
        "targets": targets,
        "agent": agent_wheel,
        "trust": {PUBLISHER: public},
    }


def build_test_release(tmp_path, version="0.6.0", *, output=None, **options):
    inputs = release_inputs(tmp_path, version, **options)
    path = build_release(
        inputs["core"],
        output or tmp_path / f"skeleton-release-{version}.skapp",
        inputs["private"],
        targets=inputs["targets"],
        agent_wheel=inputs["agent"],
    )
    return {**inputs, "release": path}


def read_archive(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def write_archive(path, members, *, manifest=None, private=None, raw=None):
    """Rewrite a package: ``manifest`` (dict) is re-signed with ``private`` unless ``raw`` is given."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    members = dict(members)
    if manifest is not None or raw is not None:
        data = raw if raw is not None else canonical_json(manifest)
        members["manifest.json"] = data
        if private is not None:
            key = load_pem_private_key(Path(private).read_bytes(), password=None)
            members["manifest.sig"] = b64encode(key.sign(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    Path(path).write_bytes(buffer.getvalue())
    return Path(path)


def manifest_of(path):
    return json.loads(read_archive(path)["manifest.json"])


def inventory_item(name, data, role):
    return {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "role": role}


def app_package(directory, private, *, publisher=PUBLISHER, app_id="org.test.demo", version="0.1.0"):
    """A minimal signed app package built like the packaging tests build theirs."""
    from skeleton.system.packages import build_package, manifest_template

    root = Path(directory) / "app-source"
    (root / "docs").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "apps.py").write_text(
        "from django.apps import AppConfig\nclass DemoConfig(AppConfig):\n"
        '    name = __package__\n    label = "project_demo"\n'
    )
    (root / "service_api.py").write_text("class DemoService:\n    pass\n")
    (root / "docs/spec.md").write_text("## Contract\nOwn directory only.\n## Acceptance\nSigned.\n")
    (root / "docs/adr.md").write_text("## Context\nX.\n## Decision\nSigned.\n## Consequences\nPrivileged.\n")
    (root / "docs/service-api.md").write_text(
        "## Serviços\nDemoService.\n## DTOs\nNenhum.\n## Histórico de versões\n0.1.0.\n", encoding="utf-8"
    )
    manifest = manifest_template(
        app_id=app_id,
        app_label="project_demo",
        package_name="package_demo",
        publisher=publisher,
        version=version,
        app_config_class="DemoConfig",
        service_classes=["DemoService"],
    )
    manifest_path = Path(directory) / "app-manifest.json"
    manifest_path.write_bytes(canonical_json(manifest))
    return build_package(root, manifest_path, Path(directory) / f"demo-{version}.skapp", private)


def signed_index(path, entries, private, *, publisher=PUBLISHER, generation=1, expires_at):
    """A signed index written directly, so that an already expired one can exist."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    catalog = {
        "catalog_version": 2,
        "publisher": publisher,
        "generation": generation,
        "expires_at": expires_at,
        "entries": entries,
    }
    key = load_pem_private_key(Path(private).read_bytes(), password=None)
    signature = b64encode(key.sign(canonical_json(catalog))).decode("ascii")
    Path(path).write_bytes(canonical_json({"catalog": catalog, "signature": signature}))
    return Path(path)
