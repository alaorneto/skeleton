"""First-party libraries installed for the apps that need them (#793), without the operator's opt-in."""

import json
import sys
import types

import pytest
from django.core.exceptions import ImproperlyConfigured

from skeleton.conf import bundled


def entry(*dependencies, state="installed"):
    return {"state": state, "manifest": {"dependencies": dict.fromkeys(dependencies, ">=0.0.0")}}


def test_the_shipped_libraries_and_their_ids():
    assert bundled.bundled_libraries() == ("calendar", "ledger")
    assert bundled.library_app_ids() == ("org.skeleton.calendar", "org.skeleton.ledger")


def test_required_libraries_follow_the_dependencies_in_map_order(monkeypatch):
    assert bundled.required_libraries(["org.skeleton.ledger", "org.example.other"]) == ("ledger",)
    assert bundled.required_libraries(["org.skeleton.ledger", "org.skeleton.calendar"]) == ("calendar", "ledger")
    assert bundled.required_libraries([]) == ()
    # A library that requires another one brings it too.
    real = bundled._shipped_manifest

    def shipped(name):
        manifest = real(name)
        if name == "ledger":
            return {**manifest, "dependencies": {"org.skeleton.calendar": ">=0.0.0"}}
        return manifest

    monkeypatch.setattr(bundled, "_shipped_manifest", shipped)
    assert bundled.required_libraries(["org.skeleton.ledger"]) == ("calendar", "ledger")


def test_a_first_party_app_keeps_its_opt_in(monkeypatch):
    real = bundled._shipped_manifest
    monkeypatch.setattr(
        bundled,
        "_shipped_manifest",
        lambda name: {**real(name), "kind": "bundled_app"} if name == "ledger" else real(name),
    )
    assert bundled.bundled_libraries() == ("calendar",)
    assert bundled.required_libraries(["org.skeleton.ledger"]) == ()


def test_installed_names_keep_the_opt_in_first_and_once():
    assert bundled.installed_bundled_names(("ledger",), ("calendar", "ledger"), ()) == ("ledger", "calendar")
    assert bundled.installed_bundled_names(("", " calendar "), ()) == ("calendar",)
    with pytest.raises(ImproperlyConfigured):
        bundled.bundled_app_configs(bundled.installed_bundled_names(("unknown",)))


def test_the_next_inventory_retains_a_library_its_last_app_left():
    previous = {"apps": [entry("org.skeleton.calendar"), entry("org.skeleton.ledger", state="schema_retained")]}
    # The app that used the calendar is gone: the calendar stays retained, never dropped.
    assert bundled.next_retained_libraries(previous, [], ()) == ["calendar"]
    # Still required, or opted in by the operator: nothing retained.
    assert bundled.next_retained_libraries(previous, [entry("org.skeleton.calendar", state="project-local")], ()) == []
    assert bundled.next_retained_libraries(previous, [], ("calendar",)) == []
    # Once retained it stays retained until an app requires it again.
    kept = {"apps": [], "retained_libraries": ["calendar"]}
    assert bundled.next_retained_libraries(kept, [], ()) == ["calendar"]
    assert bundled.next_retained_libraries(kept, [entry("org.skeleton.calendar")], ()) == []
    # A library no app ever required is never retained.
    assert bundled.next_retained_libraries({"apps": []}, [], ()) == []


def test_project_apps_bring_the_libraries_their_manifest_requires(tmp_path, monkeypatch):
    package = tmp_path / "libproject" / "tasks"
    package.mkdir(parents=True)
    (tmp_path / "libproject" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "manifest.json").write_text(
        json.dumps({"dependencies": {"org.skeleton.ledger": ">=0.0.0"}}), encoding="utf-8"
    )
    other = tmp_path / "libproject" / "plain"
    other.mkdir()
    (other / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "libproject", raising=False)
    configs = bundled.project_library_configs(
        ["libproject.tasks.apps.TasksConfig", "libproject.plain.apps.PlainConfig"]
    )
    assert configs == [bundled.BUNDLED_APPS["ledger"]]
    assert bundled.project_library_configs(["libproject.tasks.apps.TasksConfig"], installed=configs) == []
    assert isinstance(sys.modules.get("libproject"), types.ModuleType)  # Only the parent package was imported.
    assert "libproject.tasks.apps" not in sys.modules


def test_dev_app_manifests_feed_the_required_libraries(tmp_path):
    from skeleton.conf.dev_apps import dev_app_manifests

    assert dev_app_manifests(["", " "]) == []
    with pytest.raises(ImproperlyConfigured, match="não existe"):
        dev_app_manifests([str(tmp_path / "missing")])
