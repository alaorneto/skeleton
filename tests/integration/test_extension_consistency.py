"""Database-free consistency checks between an app's manifest, migrations, models and TableSpec."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from django.db import models
from django.test.utils import isolate_apps

from skeleton.system.checks import (
    extension_consistency_checks,
    undeclared_migration_dependencies,
    undeclared_relations,
)


def ids(errors):
    return sorted(error.id for error in errors)


def test_example_application_is_consistent():
    assert ids(extension_consistency_checks()) == []


def test_migration_dependencies_must_be_declared_for_registered_apps_only():
    migrations = [
        SimpleNamespace(dependencies=[("own", "0001_initial"), ("other", "0002_x"), ("sk_audit", "0002_x")]),
        SimpleNamespace(dependencies=[("declared", "0001_initial"), ("other", "0003_y")]),
    ]
    registered = {"own", "other", "declared"}
    assert undeclared_migration_dependencies(migrations, registered, {"own", "declared"}) == ["other"]
    assert undeclared_migration_dependencies(migrations, registered, {"own", "declared", "other"}) == []


@isolate_apps("skeleton.system")
def test_relations_to_registered_apps_must_be_declared():
    class Target(models.Model):
        class Meta:
            app_label = "fixture_target"

    class Source(models.Model):
        target = models.ForeignKey(Target, on_delete=models.PROTECT)

        class Meta:
            app_label = "fixture_source"

    registered = {"fixture_source", "fixture_target"}
    assert undeclared_relations([Source], registered, {"fixture_source"}) == [
        ("fixture_source.Source", "target", "fixture_target")
    ]
    assert undeclared_relations([Source], registered, registered) == []
    assert undeclared_relations([Source], {"fixture_source"}, {"fixture_source"}) == []


def test_manifest_leaves_must_match_the_migration_graph(monkeypatch):
    from skeleton.system import app_registry

    manifest = app_registry._manifests["example_notes"]
    canonical = {**manifest.canonical, "migrations": {**manifest.canonical["migrations"], "leaves": ["0099_missing"]}}
    monkeypatch.setitem(app_registry._manifests, "example_notes", replace(manifest, canonical=canonical))
    errors = [error for error in extension_consistency_checks() if error.id == "skeleton.W017"]
    assert len(errors) == 1
    assert "0099_missing" in errors[0].msg and "0001_initial" in errors[0].msg


@pytest.mark.parametrize("sensitive", [(), ("body", "title")])
def test_table_spec_sensitive_fields_must_match_the_frozen_specification(monkeypatch, sensitive):
    from skeleton.system import registry

    spec = registry._registry["example_notes.note"]
    monkeypatch.setitem(registry._registry, "example_notes.note", replace(spec, sensitive_fields=sensitive))
    errors = [error for error in extension_consistency_checks() if error.id == "skeleton.E016"]
    assert len(errors) == 1
    assert "example_notes.Note" in errors[0].msg
