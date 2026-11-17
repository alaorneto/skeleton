"""Derived authorization: a child table authorized through its parent record (#561, epic #560).

A table whose ``TableSpec`` declares ``access_parent`` is a **derived table**. Its **anchor** is the
exposed ``SecuredRecord`` at the end of the ``access_parent`` chain (at most ``MAX_HOPS`` foreign
keys). Every core point that decides whether a record can be authorized asks ``access_route``
(``is_authorizable``/``is_derived``) instead of "``SecuredRecord`` with ``expose_api``?".

Authorization is only Python: no database, RLS, trigger or frozen infrastructure changes. The
access of a derived row is never wider than the access to its anchor (docs/access-control.md,
"Tabelas derivadas").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import models

from skeleton.system import registry

# The most foreign keys between a derived table and its anchor.
MAX_HOPS = 3

# The action required on the anchor for each action asked on a derived row. ``manage_access`` is
# missing on purpose: access is managed on the anchor, never on a child. ``audit`` is decided by the
# audit history itself (``audit`` on the anchor table and ``read`` on the anchor record, #564).
DERIVED_ACTION_MAP: dict[str, str] = {
    "read": "read",
    "create": "update",
    "update": "update",
    "delete": "update",
    "restore": "update",
}


@dataclass(frozen=True)
class AccessRoute:
    """How a table is authorized.

    ``path`` is ``""`` for an anchor, ``"workspace"`` or ``"item__workspace"`` for derived tables;
    ``links`` are the lookups of the intermediate derived records (``("item",)`` in the second case),
    which must be alive for the row to be visible.
    """

    model: type
    anchor: type
    path: str
    links: tuple[str, ...]


# model -> (the specs the route was computed from, route). Revalidated by identity on every read,
# so a registry replaced in tests (or a spec popped from it) never serves a stale route.
_routes: dict[type, tuple[tuple[tuple[str, Any], ...], AccessRoute | None]] = {}


def _spec(model: type) -> Any | None:
    try:
        return registry.get_spec(model._meta.label_lower)
    except (KeyError, AttributeError):
        return None


def _is_anchor(model: type, spec: Any) -> bool:
    from skeleton.common.models import SecuredRecord

    return isinstance(model, type) and issubclass(model, SecuredRecord) and bool(spec.expose_api)


def _parent_field(model: type, spec: Any) -> models.Field | None:
    try:
        return model._meta.get_field(spec.access_parent)
    except Exception:  # noqa: BLE001 - FieldDoesNotExist and friends: the check reports it
        return None


def declaration_problems(model: type, spec: Any) -> list[str]:
    """Problems of the table's own ``access_parent`` declaration (rules 1 and 2 of the design, same app)."""
    from skeleton.common.models import OperationalRecord, OrganizationRecord, SecuredRecord

    label = model._meta.label
    problems = []
    if not isinstance(model, type) or not issubclass(model, OrganizationRecord) or issubclass(model, SecuredRecord):
        problems.append(f"{label}: access_parent is only for an OrganizationRecord that is not a SecuredRecord.")
    if isinstance(model, type) and issubclass(model, OperationalRecord):
        problems.append(f"{label}: an OperationalRecord never declares access_parent.")
    if spec.expose_api:
        problems.append(f"{label}: a derived table is never exposed; set expose_api=False.")
    if spec.scope != "organization":
        problems.append(f"{label}: a derived table has organization scope.")
    field = _parent_field(model, spec)
    if field is None or not isinstance(field, models.ForeignKey) or not field.concrete:
        problems.append(f"{label}: access_parent {spec.access_parent!r} is not a concrete ForeignKey of the model.")
        return problems
    if field.null:
        problems.append(f"{label}: the access_parent foreign key cannot be null.")
    if field.remote_field.on_delete is not models.PROTECT:
        problems.append(f"{label}: the access_parent foreign key must use on_delete=PROTECT.")
    immutable = getattr(model, "immutable_fields", ())
    if field.name not in immutable and field.attname not in immutable:
        problems.append(f"{label}: the access_parent foreign key must be in immutable_fields.")
    target = field.related_model
    if not isinstance(target, type) or target._meta.app_label != model._meta.app_label:
        problems.append(f"{label}: access_parent must point to a table of the same app.")
    return problems


def _resolve(model: type) -> tuple[tuple[tuple[str, Any], ...], AccessRoute | None, list[str]]:
    """Walk the chain: the specs read, the route (or ``None``) and the chain problems."""
    spec = _spec(model)
    if spec is None:
        label = getattr(getattr(model, "_meta", None), "label_lower", None)
        return ((label, None),) if label else (), None, ["The table has no TableSpec."]
    read = [(model._meta.label_lower, spec)]
    if not getattr(spec, "access_parent", None):
        route = AccessRoute(model, model, "", ()) if _is_anchor(model, spec) else None
        return tuple(read), route, [] if route else [f"{model._meta.label}: not an exposed SecuredRecord."]
    lookups: list[str] = []
    seen = {model}
    current, current_spec = model, spec
    while True:
        problems = declaration_problems(current, current_spec)
        if problems:
            if current is not model:
                problems = [f"{model._meta.label}: the access_parent target {current._meta.label} is not authorizable."]
            return tuple(read), None, problems
        field = _parent_field(current, current_spec)
        lookups.append(field.name)
        target = field.related_model
        if target in seen:
            return tuple(read), None, [f"{model._meta.label}: the access_parent chain has a cycle."]
        if len(lookups) > MAX_HOPS:
            return tuple(read), None, [f"{model._meta.label}: the access_parent chain has more than {MAX_HOPS} hops."]
        target_spec = _spec(target)
        unauthorizable = [f"{model._meta.label}: the access_parent target {target._meta.label} is not authorizable."]
        if target_spec is None:
            # A missing spec is part of the cache key too: registering it later recomputes the route.
            read.append((target._meta.label_lower, None))
            return tuple(read), None, unauthorizable
        read.append((target._meta.label_lower, target_spec))
        if not getattr(target_spec, "access_parent", None):
            if not _is_anchor(target, target_spec):
                return tuple(read), None, unauthorizable
            path = "__".join(lookups)
            links = tuple("__".join(lookups[: index + 1]) for index in range(len(lookups) - 1))
            return tuple(read), AccessRoute(model, target, path, links), []
        seen.add(target)
        current, current_spec = target, target_spec


def access_parent_problems(model: type, spec: Any) -> list[str]:
    """Every structural problem of a declared ``access_parent`` (the E042 check)."""
    own = declaration_problems(model, spec)
    if own:
        return own
    return _resolve(model)[2]


def access_route(model: type) -> AccessRoute | None:
    """How ``model`` is authorized, or ``None`` when it is not authorizable (fails closed)."""
    cached = _routes.get(model)
    if cached is not None and all(_spec_is(label, spec) for label, spec in cached[0]):
        return cached[1]
    read, route, _problems = _resolve(model)
    _routes[model] = (read, route)
    return route


def _spec_is(label: str, spec: Any) -> bool:
    try:
        return registry.get_spec(label) is spec
    except KeyError:
        return spec is None


def is_authorizable(model: type) -> bool:
    """An exposed ``SecuredRecord`` or a valid derived table."""
    return access_route(model) is not None


def is_derived(model: type) -> bool:
    """A valid derived table (authorized through an anchor)."""
    route = access_route(model)
    return route is not None and route.path != ""


def anchor_record(record: Any) -> Any | None:
    """The anchor record of ``record``, or ``None`` when a link or the anchor has a tombstone.

    For an anchor table, the record itself (``None`` when it has a tombstone). ``None`` also for a
    table that is not authorizable. The record's own tombstone is the caller's decision.
    """
    route = access_route(type(record))
    if route is None:
        return None
    if not route.path:
        return record if record.deleted_at is None else None
    # Read from the database, never from related objects cached on the instance: a parent deleted
    # since the record was loaded must count as deleted.
    live_links = {f"{link}__deleted_at__isnull": True for link in route.links}
    anchor_id = route.model.all_objects.filter(pk=record.pk, **live_links).values_list(route.path, flat=True).first()
    if anchor_id is None:
        return None
    return route.anchor.objects.filter(pk=anchor_id).first()
