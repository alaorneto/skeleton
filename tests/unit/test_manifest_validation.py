"""Pure manifest, version and dependency-graph validation: no signed artifact, runs on every platform."""

import json
import subprocess
import sys

import pytest

from skeleton.system.packages import (
    PackageError,
    canonical_json,
    manifest_template,
    validate_dependency_graph,
    validate_manifest,
)
from skeleton.versioning import version_satisfies


@pytest.fixture
def app_manifest():
    """The same source manifest the signed_app fixture of test_packages.py writes, as a fresh dict."""
    return manifest_template(
        app_id="org.test.demo",
        app_label="project_demo",
        package_name="package_demo",
        publisher="test-publisher",
        app_config_class="DemoConfig",
        service_classes=["DemoService"],
    )


def core_manifest(manifest):
    manifest = json.loads(canonical_json(manifest))  # independent copy
    for key in ("app_id", "app_label", "package_name", "app_config", "runtime"):
        manifest.pop(key)
    manifest.update(kind="core", component_id="django-skeleton")
    manifest["migrations"] = {"leaves": [], "from_versions": ">=0.0.0"}
    return manifest


def declare_dependency(manifest, name, version, digest, marker=None):
    wheel = f"wheels/{name.replace('-', '_')}-{version}-py3-none-any.whl"
    dependency = {"name": name, "version": version, "wheel": wheel, "sha256": digest}
    if marker is not None:
        dependency["marker"] = marker
    manifest["python_dependencies"] = [dependency]
    manifest["files"] = [{"path": wheel, "size": 0, "sha256": digest, "role": "wheel"}]


def dimension_layout(**dimensions):
    return [
        {
            "table_key": "project_demo.item",
            "form": {
                "sections": [
                    {
                        "id": "content",
                        "label": "Content",
                        "fields": [{"name": "body", "widget": "textarea", **dimensions}],
                    }
                ]
            },
        }
    ]


def test_dependency_versions_cycles_and_namespace_collisions(app_manifest):
    first = app_manifest
    second = manifest_template(
        app_id="org.test.second", app_label="project_second", package_name="second", publisher="test-publisher"
    )
    second["dependencies"] = {first["app_id"]: ">=0.1.0,<0.2.0"}
    kwargs = {"core": "0.4.0", "python": "3.14.0", "django": "6.1.3"}
    assert validate_dependency_graph([second, first], **kwargs) == [first["app_id"], second["app_id"]]
    with pytest.raises(PackageError):
        validate_dependency_graph([second], **kwargs)
    first["dependencies"] = {second["app_id"]: ">=0.1.0"}
    with pytest.raises(PackageError):
        validate_dependency_graph([second, first], **kwargs)
    first["dependencies"] = {}
    second["package_name"] = first["package_name"]
    second["app_config"] = first["app_config"]
    with pytest.raises(PackageError):
        validate_dependency_graph([first, second], **kwargs)
    with pytest.raises(PackageError):
        validate_dependency_graph([first], **{**kwargs, "django": "6.0.0"})


def test_manifest_disallows_unknown_fields_reserved_names_and_runtime_escape(app_manifest):
    base = app_manifest
    for change in (
        {"shell": "echo bad"},
        {"app_label": "sk_accounts"},
        {"runtime": {**base["runtime"], "urls": "config.urls"}},
        {"files": [{"path": "app/../other.py", "size": 0, "sha256": "0" * 64, "role": "code"}]},
    ):
        with pytest.raises(PackageError):
            validate_manifest({**base, **change})


def test_standalone_import_needs_no_django_configuration():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            'import sys; from skeleton.system.packages import manifest_template; assert "django" not in sys.modules; print("standalone")',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "standalone"


@pytest.mark.parametrize(
    "failure", ["python", "django", "skeleton", "missing", "range", "conflict", "cycle", "target-version"]
)
def test_dependency_graph_validates_complete_core_contract(app_manifest, failure):
    app = app_manifest
    core = core_manifest(app_manifest)
    core["version"] = "0.4.0"
    kwargs = {"core": "0.4.0", "python": "3.14.0", "django": "6.1.3"}
    core["dependencies"] = {app["app_id"]: ">=0.1.0,<0.2.0"}
    assert validate_dependency_graph([core, app], **kwargs) == [app["app_id"]]
    if failure in ("python", "django", "skeleton"):
        core["compatibility"][failure] = ">=99"
    elif failure == "missing":
        core["dependencies"] = {"org.test.absent": ">=0.1.0"}
    elif failure == "range":
        core["dependencies"] = {app["app_id"]: ">=0.2.0"}
    elif failure == "conflict":
        core["conflicts"] = {app["app_id"]: ">=0.1.0"}
    elif failure == "cycle":
        app["dependencies"] = {"django-skeleton": ">=0.1.0"}
    else:
        kwargs["core"] = "0.5.0"  # The core manifest is 0.4.0.
    with pytest.raises(PackageError):
        validate_dependency_graph([core, app], **kwargs)


@pytest.mark.parametrize("target", ["linux", "win32"])
def test_dependency_graph_compares_only_target_active_wheel_pins(app_manifest, target):
    first = app_manifest
    second = manifest_template(
        app_id="org.test.second", app_label="project_second", package_name="second", publisher="test-publisher"
    )
    declare_dependency(first, "dependency", "1.0.0", "a" * 64, 'sys_platform == "win32"')
    declare_dependency(second, "dependency", "2.0.0", "b" * 64, 'sys_platform == "linux"')
    kwargs = {"core": "0.4.0", "python": "3.14.0", "django": "6.1.3", "target_environment": {"sys_platform": target}}
    assert set(validate_dependency_graph([first, second], **kwargs)) == {first["app_id"], second["app_id"]}
    first["python_dependencies"][0]["marker"] = f'sys_platform == "{target}"'
    second["python_dependencies"][0]["marker"] = f'sys_platform == "{target}"'
    with pytest.raises(PackageError, match="versions or builds"):
        validate_dependency_graph([first, second], **kwargs)


@pytest.mark.parametrize("dimensions", [{"col_span": 4}, {"rows": 8}, {"col_span": None}, {"rows": None}])
@pytest.mark.parametrize("bound", [">=0.3.0,<1.0.0", ">0.3.0,<1.0.0", "==0.3.0", "~=0.3.0"])
def test_dimensions_require_explicit_new_floor(app_manifest, dimensions, bound):
    manifest = app_manifest
    manifest["runtime"].pop("service_api")  # Its own, higher floor is covered separately.
    manifest["runtime"]["model_ui"] = dimension_layout(**dimensions)
    manifest["compatibility"]["skeleton"] = ">=0.2.0,<1.0.0"
    with pytest.raises(PackageError, match=r"^Field dimensions require a core minimum of 0\.3\.0\.$"):
        validate_manifest(manifest)
    manifest["compatibility"]["skeleton"] = bound
    assert validate_manifest(manifest)["runtime"]["model_ui"] == dimension_layout(**dimensions)


def test_new_manifest_template_requires_current_presentation_core(app_manifest):
    manifest = app_manifest
    assert version_satisfies("0.4.0", manifest["compatibility"]["skeleton"])
    assert not version_satisfies("0.3.0", manifest["compatibility"]["skeleton"])
