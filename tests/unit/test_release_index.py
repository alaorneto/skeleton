"""Generated core manifest, line policy and the accumulated release index (#494)."""

import json
import tomllib
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

from skeleton import distribution
from skeleton.distribution import build_core_release, core_manifest
from skeleton.system import packages
from skeleton.system.packages import (
    PackageError,
    build_catalog,
    release_catalog_entry,
    validate_catalog,
    validate_manifest,
    validate_package,
    validate_release,
)
from skeleton.system.packaging.release import _validate_release_archive
from skeleton.versioning import upgrade_sources
from tests.unit.release_helpers import (
    PUBLISHER,
    app_package,
    build_test_release,
    core_wheel,
    private_key,
    public_pem,
    signed_index,
)


@pytest.mark.parametrize(
    "version,known,earliest,expected",
    [
        ("0.6.0", (), "0.5.0", ">=0.5.0,<=0.6.0"),
        ("0.6.3", ("0.6.0", "0.6.1"), "0.5.0", ">=0.5.0,<=0.6.3"),
        ("0.7.0", ("0.6.0", "0.6.3"), "0.5.0", ">=0.6.0,<=0.7.0"),
        ("1.0.0", ("0.6.3", "0.7.2"), None, ">=0.7.0,<=1.0.0"),
        ("1.0.1", ("0.7.2", "1.0.0"), None, ">=0.7.0,<=1.0.1"),
        ("0.6.0", (), None, ">=0.6.0,<=0.6.0"),
    ],
)
def test_upgrade_sources_policy(version, known, earliest, expected):
    assert upgrade_sources(version, known, earliest=earliest) == expected


def test_upgrade_sources_ignores_newer_versions_and_accepts_prereleases():
    assert upgrade_sources("0.7.0", ("0.6.1", "0.8.0", "0.9.2")) == ">=0.6.0,<=0.7.0"
    assert upgrade_sources("0.7.0-rc.1", ("0.6.1",)) == ">=0.6.0,<=0.7.0rc1"
    for invalid in ("0.7", "x"):
        with pytest.raises(ValueError):
            upgrade_sources(invalid)


def test_upgrade_sources_refuses_an_earliest_source_above_the_version():
    """Regression (#650): the floor of the first automated release cannot make an empty range."""
    with pytest.raises(ValueError, match=r"Earliest source 0\.5\.0 is above the version 0\.3\.0\."):
        upgrade_sources("0.3.0", (), earliest="0.5.0")
    assert upgrade_sources("0.3.0", (), earliest="0.3.0") == ">=0.3.0,<=0.3.0"
    # A known earlier line wins over the floor, as before.
    assert upgrade_sources("0.3.0", ("0.2.0",), earliest="0.5.0") == ">=0.2.0,<=0.3.0"


def test_versioned_recipe_floor_is_the_base_release():
    """Regression (#650): the floor is the first published release, never above the project version."""
    root = Path(__file__).resolve().parents[2]
    recipe = json.loads((root / "deploy" / "release" / "recipe.json").read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert recipe["earliest_source"] == "0.1.0"
    assert upgrade_sources(project, (), earliest=recipe["earliest_source"]) == f">=0.1.0,<={project}"


def wheel(tmp_path, **options):
    return core_wheel(tmp_path / "wheel", "0.7.0", **options)


@pytest.mark.posix
def test_core_manifest_follows_the_wheel_and_the_policy(tmp_path):
    path = wheel(tmp_path)
    manifest = core_manifest(path, publisher=PUBLISHER, known_versions=["0.6.0", "0.6.3"], earliest_source="0.5.0")
    policy = SpecifierSet(">=0.6.0,<=0.7.0")
    assert (
        SpecifierSet(manifest["migrations"]["from_versions"])
        == SpecifierSet(manifest["compatibility"]["skeleton"])
        == policy
    )
    assert SpecifierSet(manifest["compatibility"]["python"]) == SpecifierSet(">=3.12,<3.15")
    assert SpecifierSet(manifest["compatibility"]["django"]) == SpecifierSet(">=6.1,<6.2")
    assert manifest["files"] == [] and manifest["version"] == "0.7.0"
    assert manifest["python_dependencies"][0]["wheel"] == "wheels/" + path.name
    inventoried = {
        **manifest,
        "files": [
            {
                "path": "wheels/" + path.name,
                "size": path.stat().st_size,
                "sha256": manifest["python_dependencies"][0]["sha256"],
                "role": "wheel",
            }
        ],
    }
    validate_manifest(inventoried)
    private, public = private_key(tmp_path / "publisher.pem")
    source = tmp_path / "core-manifest.json"
    source.write_text(json.dumps(manifest))
    package = build_core_release(path, source, tmp_path / "core.skapp", private)
    verified = validate_package(package, {PUBLISHER: public})
    assert SpecifierSet(verified.manifest["migrations"]["from_versions"]) == policy


@pytest.mark.posix
@pytest.mark.parametrize("requires", [(), ("Django>=6.1",), ("Django==6.*",), ("Django==6.1.3; python_version>'3'",)])
def test_core_manifest_requires_an_exact_django_pin(tmp_path, requires):
    with pytest.raises(PackageError, match="pin Django"):
        core_manifest(wheel(tmp_path, requires=requires), publisher=PUBLISHER)


@pytest.mark.posix
def test_core_manifest_accepts_a_two_part_django_pin(tmp_path):
    manifest = core_manifest(wheel(tmp_path, requires=("Django==6.2",)), publisher=PUBLISHER)
    assert SpecifierSet(manifest["compatibility"]["django"]) == SpecifierSet(">=6.2,<6.3")


@pytest.mark.posix
def test_skeleton_core_manifest_never_overwrites(tmp_path, capsys):
    path = wheel(tmp_path)
    notes = tmp_path / "notes.md"
    notes.write_text("Notas do release.", encoding="utf-8")
    output = tmp_path / "manifest.json"
    arguments = [
        "manifest",
        "--wheel",
        str(path),
        "--publisher",
        PUBLISHER,
        "--output",
        str(output),
        "--known-version",
        "0.6.0",
        "--earliest-source",
        "0.5.0",
        "--release-notes-file",
        str(notes),
        "--build",
        "ci",
    ]
    distribution.main(arguments)
    assert capsys.readouterr().out.strip() == str(output)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert (written["release_notes"], written["build"]) == ("Notas do release.", "ci")
    before = output.read_bytes()
    with pytest.raises(SystemExit) as raised:
        distribution.main(arguments)
    assert raised.value.code == 1 and output.read_bytes() == before


def future(days=7):
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


@pytest.fixture
def published(tmp_path):
    built = build_test_release(tmp_path)
    store = tmp_path / "trust.json"
    store.write_text(json.dumps({PUBLISHER: str(public_pem(built["public"], tmp_path / "public.pem"))}))
    return {**built, "store": store}


@pytest.mark.posix
def test_release_catalog_entry_describes_the_core(published, tmp_path):
    verified = validate_release(published["release"], published["trust"])
    entry = release_catalog_entry(verified, artifact="v0.6.0/" + published["release"].name)
    assert (entry["component_id"], entry["kind"], entry["version"]) == ("django-skeleton", "core", "0.6.0")
    assert entry["digest"] == verified.digest
    assert entry["migrations"] == verified.core_manifest["migrations"]
    assert entry["compatibility"] == verified.core_manifest["compatibility"]
    assert entry["signature"] == zipfile.ZipFile(published["release"]).read("manifest.sig").decode("ascii")
    index = build_catalog(
        [entry], tmp_path / "index.json", published["private"], publisher=PUBLISHER, generation=1, expires_at=future()
    )
    assert (
        validate_catalog(index, published["trust"])["entries"][0]["artifact"] == "v0.6.0/" + published["release"].name
    )


@pytest.mark.posix
def test_release_catalog_entry_refuses_untrusted_or_changed_packages(published):
    untrusted = _validate_release_archive(published["release"], None)
    with pytest.raises(PackageError, match="trusted verified"):
        release_catalog_entry(untrusted, artifact="x.skapp")
    verified = validate_release(published["release"], published["trust"])
    published["release"].write_bytes(published["release"].read_bytes() + b"\0")
    with pytest.raises(PackageError):
        release_catalog_entry(verified, artifact="x.skapp")


def index(published, *artifacts, output, generation, previous=None, base="v0.6.0", expires=None):
    arguments = [
        "index",
        *map(str, artifacts),
        "--output",
        str(output),
        "--private-key",
        str(published["private"]),
        "--trust-store",
        str(published["store"]),
        "--publisher",
        PUBLISHER,
        "--expires-at",
        expires or future(),
        "--generation",
        str(generation),
        "--artifact-base",
        base,
    ]
    if previous:
        arguments += ["--previous", str(previous)]
    packages.main(arguments)
    return validate_catalog(output, published["trust"])


@pytest.mark.posix
def test_index_with_release_and_app_packages(published, tmp_path, capsys):
    app = app_package(tmp_path, published["private"])
    body = index(published, published["release"], app, output=tmp_path / "index.json", generation=3)
    assert json.loads(capsys.readouterr().out)["generation"] == 3
    assert [(e["component_id"], e["kind"]) for e in body["entries"]] == [
        ("django-skeleton", "core"),
        ("org.test.demo", "app"),
    ]
    assert body["entries"][0]["artifact"] == "v0.6.0/" + published["release"].name


@pytest.mark.posix
def test_index_previous_keeps_adds_and_replaces(published, tmp_path):
    first = index(published, published["release"], output=tmp_path / "first.json", generation=1)
    old_core = dict(first["entries"][0])
    app = app_package(tmp_path, published["private"])
    second = index(
        published,
        app,
        published["release"],
        output=tmp_path / "second.json",
        generation=2,
        previous=tmp_path / "first.json",
        base="v0.6.1",
    )
    assert [e["component_id"] for e in second["entries"]] == ["django-skeleton", "org.test.demo"]
    assert second["entries"][0]["artifact"] == "v0.6.1/" + published["release"].name != old_core["artifact"]
    assert second["generation"] == 2


@pytest.mark.posix
def test_index_previous_refuses_rollback_foreign_publisher_and_bad_signature(published, tmp_path):
    index(published, published["release"], output=tmp_path / "first.json", generation=5)
    for generation in (5, 4):
        with pytest.raises(SystemExit):
            index(
                published,
                output=tmp_path / f"g{generation}.json",
                generation=generation,
                previous=tmp_path / "first.json",
            )
    other, _ = private_key(tmp_path / "other.pem")
    forged = signed_index(tmp_path / "forged.json", [], other, generation=1, expires_at=future())
    with pytest.raises(SystemExit):
        index(published, output=tmp_path / "f.json", generation=9, previous=forged)
    foreign = signed_index(
        tmp_path / "foreign.json", [], published["private"], publisher="someone", expires_at=future()
    )
    store = json.loads(published["store"].read_text())
    published["store"].write_text(json.dumps({**store, "someone": store[PUBLISHER]}))
    with pytest.raises(SystemExit):
        index(published, output=tmp_path / "o.json", generation=9, previous=foreign)
    (tmp_path / "not-index.json").write_text("[1, 2]")
    with pytest.raises(SystemExit):
        index(published, output=tmp_path / "n.json", generation=9, previous=tmp_path / "not-index.json")


@pytest.mark.posix
def test_index_previous_accepts_an_expired_index_and_renews_without_artifacts(published, tmp_path):
    entry = release_catalog_entry(validate_release(published["release"], published["trust"]), artifact="v0.6.0/r.skapp")
    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    expired = signed_index(
        tmp_path / "expired.json", [entry], published["private"], generation=4, expires_at=expired_at
    )
    with pytest.raises(PackageError, match="expired"):
        validate_catalog(expired, published["trust"])
    assert validate_catalog(expired, published["trust"], allow_expired=True)["generation"] == 4
    renewed = index(published, output=tmp_path / "renewed.json", generation=5, previous=expired)
    assert renewed["entries"] == [entry] and renewed["generation"] == 5


@pytest.mark.posix
def test_index_without_previous_requires_an_artifact(published, tmp_path):
    with pytest.raises(SystemExit):
        index(published, output=tmp_path / "empty.json", generation=1)
    assert not (tmp_path / "empty.json").exists()
