"""Derived tables without a database (#561): routes, the action map, cycles, depth and the E042 check."""

import pytest
from django.db import models
from django.test.utils import isolate_apps

from skeleton.access import derivation
from skeleton.common.models import OperationalRecord, OrganizationRecord, SecuredRecord
from skeleton.system import registry
from skeleton.system.checks import record_contract_checks


@pytest.fixture
def tables(monkeypatch):
    """Define models in an isolated registry and register their specs in a copy of the spec registry."""
    monkeypatch.setattr(registry, "_registry", dict(registry._registry))
    with isolate_apps("skeleton.system.apps.SystemConfig", "skeleton.access.apps.AccessConfig") as isolated:
        counter = iter(range(1000))

        def define(base=OrganizationRecord, *, app_label="sk_system", spec=None, **attributes):
            name = f"Derived{next(counter)}"
            body = {
                "__module__": __name__,
                "Meta": type("Meta", (base.Meta,), {"app_label": app_label}),
                **attributes,
            }
            model = type(name, (base,), body)
            values = {"description": "x", "expose_api": base is SecuredRecord, **(spec or {})}
            registry.register(registry.TableSpec(model=model, **values))
            return model

        define.isolated = isolated
        yield define


def child(define, parent, *, field="parent", spec=None, null=False, on_delete=models.PROTECT, immutable=True, **extra):
    return define(
        spec={"access_parent": field, **(spec or {})},
        immutable_fields=(f"{field}_id",) if immutable else (),
        **{field: models.ForeignKey(parent, on_delete=on_delete, null=null, related_name="+")},
        **extra,
    )


def e042(define):
    return [error for error in record_contract_checks(apps_registry=define.isolated) if error.id == "skeleton.E042"]


def test_action_map_never_grants_access_management():
    assert derivation.DERIVED_ACTION_MAP == {
        "read": "read",
        "create": "update",
        "update": "update",
        "delete": "update",
        "restore": "update",
    }
    assert "manage_access" not in derivation.DERIVED_ACTION_MAP


def test_routes_up_to_three_hops(tables):
    anchor = tables(SecuredRecord)
    first = child(tables, anchor)
    second = child(tables, first, field="up")
    third = child(tables, second, field="top")
    assert derivation.access_route(anchor) == derivation.AccessRoute(anchor, anchor, "", ())
    assert derivation.access_route(first).path == "parent"
    assert derivation.access_route(second) == derivation.AccessRoute(second, anchor, "up__parent", ("up",))
    assert derivation.access_route(third) == derivation.AccessRoute(
        third, anchor, "top__up__parent", ("top", "top__up")
    )
    fourth = child(tables, third, field="far")
    assert derivation.access_route(fourth) is None
    assert any("more than 3 hops" in error.msg for error in e042(tables))
    assert derivation.is_derived(third) and not derivation.is_derived(anchor) and not derivation.is_authorizable(fourth)


def test_a_valid_declaration_has_no_error_and_unexposed_tables_are_not_authorizable(tables):
    anchor = tables(SecuredRecord)
    child(tables, anchor)
    hidden = tables(SecuredRecord, spec={"expose_api": False})
    plain = tables()
    assert e042(tables) == []
    assert derivation.access_route(hidden) is None and derivation.access_route(plain) is None


def test_the_route_follows_the_registry(tables):
    anchor = tables(SecuredRecord)
    item = child(tables, anchor)
    assert derivation.is_derived(item)
    spec = registry._registry.pop(anchor._meta.label_lower)
    assert derivation.access_route(item) is None  # A popped spec is never served from the cache.
    registry._registry[anchor._meta.label_lower] = spec
    assert derivation.is_derived(item)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda t, a: t(spec={"access_parent": "missing"}), "not a concrete ForeignKey"),
        (lambda t, a: child(t, a, null=True), "cannot be null"),
        (lambda t, a: child(t, a, on_delete=models.CASCADE), "on_delete=PROTECT"),
        (lambda t, a: child(t, a, immutable=False), "immutable_fields"),
        (lambda t, a: child(t, t(SecuredRecord, app_label="sk_access")), "same app"),
        (lambda t, a: child(t, t()), "not authorizable"),
        (lambda t, a: child(t, t(SecuredRecord, spec={"expose_api": False})), "not authorizable"),
        (lambda t, a: child(t, a, spec={"expose_api": True}), "never exposed"),
        (lambda t, a: child(t, a, spec={"scope": "global"}), "organization scope"),
        (lambda t, a: child(t, a, base=SecuredRecord), "not a SecuredRecord"),
    ],
)
def test_each_broken_declaration_is_reported(tables, build, message):
    anchor = tables(SecuredRecord)
    model = build(tables, anchor)
    errors = e042(tables)
    assert any(message in error.msg for error in errors), [error.msg for error in errors]
    assert all(error.hint and "docs/checks.md" in error.hint for error in errors)
    assert derivation.access_route(model) is None


def test_an_operational_record_never_declares_access_parent(tables):
    anchor = tables(SecuredRecord)
    model = tables(
        OperationalRecord,
        spec={"access_parent": "parent"},
        parent=models.ForeignKey(anchor, on_delete=models.PROTECT, related_name="+"),
        immutable_fields=("parent_id",),
    )
    assert derivation.access_route(model) is None
    assert any("OperationalRecord" in error.msg for error in e042(tables))


def test_a_cycle_is_reported(tables):
    first = tables(spec={"access_parent": "other"}, immutable_fields=("other_id",))
    second = child(tables, first, field="back")
    first.add_to_class("other", models.ForeignKey(second, on_delete=models.PROTECT, related_name="+"))
    assert derivation.access_route(first) is None and derivation.access_route(second) is None
    assert any("cycle" in error.msg for error in e042(tables))
