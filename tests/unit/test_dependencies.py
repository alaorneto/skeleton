"""Structured dependency resolution, independent of built artifacts and of the platform."""

import pytest

from skeleton.system.dependencies import dependency_closure, dependents_of, resolve_dependencies
from skeleton.system.packages import (
    PackageError,
    _catalog,
    manifest_template,
    validate_dependency_graph,
    validate_manifest,
)

TARGET = {"core": "0.3.0", "python": "3.14.0", "django": "6.1.3"}


def app(name, version="0.1.0", **dependencies):
    manifest = manifest_template(
        app_id=f"org.test.{name}",
        app_label=f"project_{name}",
        package_name=name,
        publisher="test-publisher",
        version=version,
    )
    manifest["dependencies"] = {f"org.test.{key}": spec for key, spec in dependencies.items()}
    return manifest


def codes(resolution):
    return [problem.code for problem in resolution.problems]


def test_order_is_dependency_first_and_matches_legacy_wrapper():
    base, middle, top = app("base"), app("middle", base=">=0.1"), app("top", middle=">=0.1", base=">=0.1")
    resolution = resolve_dependencies([top, middle, base], **TARGET)
    assert resolution.ok
    assert resolution.order == ("org.test.base", "org.test.middle", "org.test.top")
    assert validate_dependency_graph([top, base, middle], **TARGET) == list(resolution.order)


def test_reports_every_problem_with_structured_details():
    present = app("present", "0.1.0")
    consumer = app("consumer", absent=">=1.0", present=">=0.2,<1.0")
    consumer["conflicts"] = {"org.test.present": "<0.5"}
    consumer["compatibility"]["django"] = ">=7.0"
    resolution = resolve_dependencies([consumer, present], **TARGET)
    assert codes(resolution) == [
        "incompatible_platform",
        "missing_dependency",
        "incompatible_dependency_version",
        "conflicting_release",
    ]
    platform, missing, version, conflict = (problem.as_dict() for problem in resolution.problems)
    assert platform == {
        "code": "incompatible_platform",
        "component": "org.test.consumer",
        "dependency": "django",
        "required": ">=7.0",
        "found": "6.1.3",
    }
    assert missing == {
        "code": "missing_dependency",
        "component": "org.test.consumer",
        "dependency": "org.test.absent",
        "required": ">=1.0",
    }
    assert version["found"] == "0.1.0" and version["required"] == ">=0.2,<1.0"
    assert conflict["dependency"] == "org.test.present"
    with pytest.raises(PackageError, match="Incompatible django version for org.test.consumer"):
        validate_dependency_graph([consumer, present], **TARGET)


def test_cycle_is_reported_once_with_its_path():
    first, second = app("first", second=">=0"), app("second", first=">=0")
    resolution = resolve_dependencies([first, second], **TARGET)
    assert codes(resolution) == ["dependency_cycle"]
    assert resolution.problems[0].path == ("org.test.first", "org.test.second", "org.test.first")
    with pytest.raises(PackageError, match="Cyclic component dependency"):
        validate_dependency_graph([first, second], **TARGET)


def test_namespace_collision_is_a_problem_not_a_crash():
    first, second = app("first"), app("second")
    second["package_name"] = first["package_name"]
    second["app_config"] = first["app_config"]
    assert codes(resolve_dependencies([first, second], **TARGET)) == ["namespace_collision"]


def test_dependents_and_closure_are_transitive():
    base, middle, top, other = app("base"), app("middle", base=">=0"), app("top", middle=">=0"), app("other")
    manifests = [base, middle, top, other]
    assert dependents_of(manifests, ["org.test.base"]) == ["org.test.middle", "org.test.top"]
    assert dependents_of(manifests, ["org.test.base", "org.test.middle"]) == ["org.test.top"]
    assert dependents_of(manifests, ["org.test.other"]) == []
    assert dependency_closure(manifests, "org.test.top") == ["org.test.base", "org.test.middle"]


def test_applications_cannot_depend_on_the_core_component():
    manifest = app("consumer")
    manifest["dependencies"] = {"django-skeleton": ">=0.3.0"}
    with pytest.raises(PackageError, match="compatibility.skeleton"):
        validate_manifest(manifest)


def catalog(version, **extra):
    entry = {
        "component_id": "org.test.base",
        "kind": "app",
        "version": "0.1.0",
        "channel": "stable",
        "compatibility": {"skeleton": ">=0.3.0,<1.0.0", "python": ">=3.12,<3.15", "django": ">=5.2,<5.3"},
        "artifact": "base.skapp",
        "digest": "a" * 64,
        "signature": "A" * 86 + "==",
        "publisher": "test-publisher",
        "notes": "",
        "migrations": {"leaves": [], "from_versions": ">=0.0.0"},
        **extra,
    }
    return {
        "catalog_version": version,
        "publisher": "test-publisher",
        "generation": 1,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "entries": [entry],
    }


def test_catalog_v2_carries_dependency_ranges_and_v1_stays_valid():
    assert _catalog(catalog(1))["catalog_version"] == 1
    v2 = _catalog(catalog(2, dependencies={"org.test.lib": ">=1.0"}, conflicts={}))
    assert v2["entries"][0]["dependencies"] == {"org.test.lib": ">=1.0"}
    with pytest.raises(PackageError):
        _catalog(catalog(1, dependencies={}, conflicts={}))
    with pytest.raises(PackageError):
        _catalog(catalog(2))
    with pytest.raises(PackageError):
        _catalog(catalog(2, dependencies={"org.test.lib": "not a range"}, conflicts={}))
    with pytest.raises(PackageError):
        _catalog(catalog(3, dependencies={}, conflicts={}))


def write_migration(directory, name, dependencies):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "__init__.py").write_text("")
    (directory / f"{name}.py").write_text(
        "from django.conf import settings\nfrom django.db import migrations\n\n"
        f"class Migration(migrations.Migration):\n    dependencies = {dependencies}\n    operations = []\n"
    )


def test_static_leaves_follow_branches_merges_and_ignore_other_apps(tmp_path):
    from skeleton.system.packages import static_migration_leaves

    migrations = tmp_path / "migrations"
    write_migration(migrations, "0001_initial", '[("sk_audit", "0002_record_infrastructure")]')
    write_migration(migrations, "0002_left", '[("demo", "0001_initial")]')
    write_migration(
        migrations,
        "0002_right",
        '[("demo", "0001_initial"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]',
    )
    assert static_migration_leaves(migrations, "demo") == {"0002_left", "0002_right"}
    write_migration(migrations, "0003_merge", '[("demo", "0002_left"), ("demo", "0002_right")]')
    assert static_migration_leaves(migrations, "demo") == {"0003_merge"}
    assert static_migration_leaves(tmp_path / "absent", "demo") == set()


def test_static_leaves_are_undetermined_for_computed_dependencies(tmp_path):
    from skeleton.system.packages import static_migration_leaves

    migrations = tmp_path / "migrations"
    write_migration(migrations, "0001_initial", "[]")
    write_migration(migrations, "0002_dynamic", "list(DEPENDENCIES)")
    assert static_migration_leaves(migrations, "demo") is None


def test_build_rejects_manifest_leaves_that_differ_from_migration_files(tmp_path):
    from skeleton.system.packages import build_package, canonical_json

    root = tmp_path / "demo"
    write_migration(root / "migrations", "0001_initial", "[]")
    manifest = app("demo")
    manifest["migrations"]["leaves"] = []
    (tmp_path / "manifest.json").write_bytes(canonical_json(manifest))
    with pytest.raises(
        PackageError, match=r"migrations.leaves \[\] differ from the migration files \['0001_initial'\]"
    ):
        build_package(root, tmp_path / "manifest.json", tmp_path / "out.skapp", tmp_path / "unused.pem")
    assert not (tmp_path / "out.skapp").exists()


def test_catalog_candidates_include_prereleases_in_range():
    from skeleton.system.dependencies import catalog_candidates

    entries = [{"component_id": "org.test.base", "version": "1.2.0rc1", "compatibility": {"core": ">=0.1"}}]
    assert catalog_candidates(entries, "org.test.base", ">=1.0", {"core": "0.3.0"}) == entries


def test_app_prerelease_satisfies_dependent_range():
    base, consumer = app("base", "1.2.0-rc.1"), app("consumer", base=">=1.0.0,<2.0.0")
    resolution = resolve_dependencies([consumer, base], **TARGET)
    assert resolution.ok, codes(resolution)


CALENDAR = "org.skeleton.calendar"
BILLING_TARGET = {"core": "0.5.0", "python": "3.14.0", "django": "6.1.3"}


def billing(spec=">=0.5.0,<0.6.0"):
    manifest = app("billing")
    manifest["compatibility"]["skeleton"] = ">=0.5.0,<0.7.0"
    manifest["dependencies"] = {CALENDAR: spec}
    return manifest


def test_provided_first_party_satisfies_dependency():
    """Regression (#254): an installed first-party app satisfies a managed app's dependency."""
    resolution = resolve_dependencies([billing()], **BILLING_TARGET, provided=(CALENDAR,))
    assert resolution.ok
    assert resolution.order == ("org.test.billing",)
    assert validate_dependency_graph([billing()], **BILLING_TARGET, provided=(CALENDAR,)) == ["org.test.billing"]


def test_first_party_not_provided_is_missing_dependency():
    resolution = resolve_dependencies([billing()], **BILLING_TARGET)
    assert codes(resolution) == ["missing_dependency"]
    assert resolution.problems[0].dependency == CALENDAR


def test_provided_first_party_outside_range_is_incompatible():
    resolution = resolve_dependencies([billing()], **{**BILLING_TARGET, "core": "0.6.0"}, provided=(CALENDAR,))
    assert codes(resolution) == ["incompatible_dependency_version"]
    problem = resolution.problems[0]
    assert (problem.dependency, problem.required, problem.found) == (CALENDAR, ">=0.5.0,<0.6.0", "0.6.0")


def test_provided_first_party_is_not_in_order():
    base = app("base")
    consumer = billing()
    consumer["dependencies"]["org.test.base"] = ">=0.1"
    resolution = resolve_dependencies([consumer, base], **BILLING_TARGET, provided=(CALENDAR,))
    assert resolution.ok
    assert resolution.order == ("org.test.base", "org.test.billing")


def test_third_party_cannot_reuse_provided_identity():
    impostor = app("impostor")
    impostor["app_id"] = CALENDAR
    impostor["compatibility"]["skeleton"] = ">=0.5.0,<0.6.0"
    resolution = resolve_dependencies([impostor], **BILLING_TARGET, provided=(CALENDAR,))
    assert codes(resolution) == ["identity_collision"]
    assert resolution.problems[0].component == CALENDAR
    assert resolution.order == ()


def test_conflict_with_provided_first_party_is_reported():
    consumer = app("consumer")
    consumer["compatibility"]["skeleton"] = ">=0.5.0,<0.6.0"
    consumer["conflicts"] = {CALENDAR: "<1.0"}
    resolution = resolve_dependencies([consumer], **BILLING_TARGET, provided=(CALENDAR,))
    assert codes(resolution) == ["conflicting_release"]
    assert resolution.problems[0].found == "0.5.0"
    assert resolve_dependencies([consumer], **BILLING_TARGET).ok  # Not installed: no conflict.
