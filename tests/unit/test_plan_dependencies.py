"""Install-time dependency checks run before any release copy, extraction or lock work."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from deployment_agent.releases import (  # noqa: E402
    check_plan_dependencies,
    ordered_entries,
    target_django_version,
)
from deployment_agent.service import DeploymentService  # noqa: E402
from deployment_agent.storage import AgentError, Store, atomic_json  # noqa: E402
from skeleton.system.packages import build_catalog, manifest_template  # noqa: E402

ENVIRONMENT = {"python_full_version": "3.14.0", "python_version": "3.14", "sys_platform": "linux"}
DJANGO_HASH = "d" * 64


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


def core():
    manifest = app("core", "0.3.0")
    for key in ("app_id", "app_label", "package_name", "app_config", "runtime"):
        manifest.pop(key)
    manifest.update(kind="core", component_id="django-skeleton")
    manifest["migrations"] = {"leaves": [], "from_versions": ">=0.0.0"}
    return manifest


def inventory(*apps):
    return {
        "generation": 1,
        "core": {"version": "0.3.0", "manifest": core()},
        "apps": [{"manifest": manifest, "state": "installed"} for manifest in apps],
    }


@pytest.fixture
def agent(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text(f"django==6.1.3 --hash=sha256:{DJANGO_HASH}\n")
    config = {"requirements_lock": str(lock)}
    backend = SimpleNamespace(target_environment=lambda: dict(ENVIRONMENT))
    return config, backend


def test_missing_dependency_fails_with_details_and_catalogue_suggestions(agent):
    config, backend = agent
    calls = []

    def suggest(dependency, required, target):
        calls.append((dependency, required, target))
        return [{"component_id": dependency, "version": "1.2.0", "digest": "a" * 64}]

    with pytest.raises(AgentError) as raised:
        check_plan_dependencies(config, backend, inventory(), [app("consumer", base=">=1.0")], [], suggest=suggest)
    error = raised.value
    assert (error.code, error.status) == ("dependency_unsatisfied", 409)
    assert error.details == [
        {
            "code": "missing_dependency",
            "component": "org.test.consumer",
            "dependency": "org.test.base",
            "required": ">=1.0",
            "suggestions": [{"component_id": "org.test.base", "version": "1.2.0", "digest": "a" * 64}],
        }
    ]
    assert calls == [("org.test.base", ">=1.0", {"skeleton": "0.3.0", "python": "3.14.0", "django": "6.1.3"})]


def test_dependency_already_installed_or_in_the_same_plan_passes(agent):
    config, backend = agent
    base, consumer = app("base", "1.0.0"), app("consumer", base=">=1.0")
    assert check_plan_dependencies(config, backend, inventory(base), [consumer], []).ok
    resolution = check_plan_dependencies(config, backend, inventory(), [consumer, base], [])
    assert resolution.order == ("org.test.base", "org.test.consumer")


def test_uninstall_reports_installed_dependents_unless_removed_together(agent):
    config, backend = agent
    base, middle, top = app("base"), app("middle", base=">=0"), app("top", middle=">=0")
    current = inventory(base, middle, top)
    with pytest.raises(AgentError) as raised:
        check_plan_dependencies(config, backend, current, [], ["org.test.base"])
    assert raised.value.code == "installed_dependents"
    assert {detail["component"] for detail in raised.value.details} == {"org.test.middle", "org.test.top"}
    assert check_plan_dependencies(config, backend, current, [], ["org.test.base", "org.test.middle", "org.test.top"])


def test_update_outside_an_installed_dependent_range_names_the_dependent(agent):
    config, backend = agent
    current = inventory(app("base", "1.0.0"), app("consumer", base=">=1.0,<2.0"))
    with pytest.raises(AgentError) as raised:
        check_plan_dependencies(config, backend, current, [app("base", "2.0.0")], [])
    assert raised.value.details[0] == {
        "code": "incompatible_dependency_version",
        "component": "org.test.consumer",
        "dependency": "org.test.base",
        "required": ">=1.0,<2.0",
        "found": "2.0.0",
    }


def test_platform_incompatibility_keeps_the_runtime_code(agent):
    config, backend = agent
    future = app("future")
    future["compatibility"]["django"] = ">=7.0"
    with pytest.raises(AgentError) as raised:
        check_plan_dependencies(config, backend, inventory(), [future], [])
    assert raised.value.code == "target_runtime_incompatible"
    assert raised.value.details[0]["dependency"] == "django"


def test_target_django_version_follows_lock_then_active_manifest_pins(agent):
    config, _ = agent
    assert target_django_version(config, [app("plain")], ENVIRONMENT) == "6.1.3"
    pinned = app("pinned")
    pinned["python_dependencies"] = [
        {"name": "Django", "version": "6.1.4", "wheel": "wheels/django-6.1.4-py3-none-any.whl", "sha256": "e" * 64}
    ]
    assert target_django_version(config, [pinned], ENVIRONMENT) == "6.1.4"


def test_ordered_entries_follow_the_resolution_then_the_rest():
    entries = {name: {"id": name} for name in ("c", "retained", "a", "b")}
    assert [entry["id"] for entry in ordered_entries(entries, ("b", "a", "c"))] == ["b", "a", "c", "retained"]


def signed_catalogue(tmp_path, entries):
    key = Ed25519PrivateKey.generate()
    private = tmp_path / "catalogue.pem"
    private.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    path = build_catalog(
        entries,
        tmp_path / "catalogue.json",
        private,
        publisher="test-publisher",
        generation=1,
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    return json.loads(path.read_text()), {"test-publisher": key.public_key()}


def catalogue_entry(version, *, django=">=5.2,<5.3"):
    return {
        "component_id": "org.test.base",
        "kind": "app",
        "version": version,
        "channel": "stable",
        "compatibility": {"skeleton": ">=0.3.0,<1.0.0", "python": ">=3.12,<3.15", "django": django},
        "artifact": f"base-{version}.skapp",
        "digest": version.replace(".", "")[:1] * 64,
        "signature": "A" * 86 + "==",
        "publisher": "test-publisher",
        "notes": "",
        "migrations": {"leaves": [], "from_versions": ">=0.0.0"},
        "dependencies": {},
        "conflicts": {},
    }


def test_catalogue_suggestions_filter_range_and_target_newest_first(tmp_path):
    import hashlib

    envelope, trust = signed_catalogue(
        tmp_path,
        [
            catalogue_entry("0.9.0"),
            catalogue_entry("1.1.0"),
            catalogue_entry("1.4.0"),
            catalogue_entry("1.5.0", django=">=6.0"),
        ],
    )
    store = Store(tmp_path / "journal")
    cache = store.root / "catalogues" / f"{hashlib.sha256(b'main').hexdigest()}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(cache, {"generation": 1, "envelope": envelope})
    service = SimpleNamespace(
        store=store,
        config={"approved_publishers": trust},
        catalogue_sources=lambda: [{"id": "main"}],
    )
    target = {"skeleton": "0.3.0", "python": "3.14.0", "django": "5.2.18"}
    suggestions = DeploymentService.catalogue_suggestions(service, "org.test.base", ">=1.0", target)
    assert [item["version"] for item in suggestions] == ["1.4.0", "1.1.0"]
    assert suggestions[0]["source_id"] == "main" and suggestions[0]["dependencies"] == {}


def _calendar_consumer():
    consumer = app("consumer")
    consumer["dependencies"] = {"org.skeleton.calendar": ">=0.3.0,<0.4.0"}
    return consumer


def _runtime_env(tmp_path, content):
    path = tmp_path / "runtime.env"
    path.write_text(content)
    path.chmod(0o600)
    return str(path)


def test_plan_accepts_dependency_on_installed_first_party_app(agent, tmp_path):
    """Regression (#254): the plan check reads SKELETON_BUNDLED_APPS from the runtime env file."""
    config, backend = agent
    config = {**config, "runtime_env_file": _runtime_env(tmp_path, "SKELETON_BUNDLED_APPS=calendar\n")}
    resolution = check_plan_dependencies(config, backend, inventory(), [_calendar_consumer()], [])
    assert resolution.order == ("org.test.consumer",)


def test_plan_refuses_dependency_on_first_party_app_not_installed(agent, tmp_path):
    config, backend = agent
    config = {**config, "runtime_env_file": _runtime_env(tmp_path, "DEBUG=false\n")}
    with pytest.raises(AgentError) as raised:
        check_plan_dependencies(config, backend, inventory(), [_calendar_consumer()], [])
    assert raised.value.code == "dependency_unsatisfied"
    assert [detail["code"] for detail in raised.value.details] == ["missing_dependency"]
    config["runtime_env_file"] = _runtime_env(tmp_path, "SKELETON_BUNDLED_APPS=inexistente\n")
    with pytest.raises(AgentError) as raised:
        check_plan_dependencies(config, backend, inventory(), [_calendar_consumer()], [])
    assert raised.value.code == "unknown_bundled_app"
