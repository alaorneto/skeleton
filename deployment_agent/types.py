"""Shapes of the JSON documents the agent journals (#347).

``TypedDict`` only names the keys: at run time each value is still the plain ``dict`` that is
written to and read from the journal, so the JSON format does not change. Manifests, inventories
and owned-file snapshots stay ``JSON`` (``dict[str, Any]``): their shape is the core's.
"""

import os
from typing import Any, TypedDict

JSON = dict[str, Any]
StrPath = str | os.PathLike[str]


class Binding(TypedDict):
    """What an approval binds: written by submit, compared on every redelivery."""

    plan_id: str
    plan_digest: str
    generation: int
    actor_id: str
    request_id: str


class Event(TypedDict, total=False):
    stage: str
    at: str
    log: str  # Only the file name under logs/<operation>/ (#244).


class Plan(TypedDict, total=False):
    """The plan a preview prepares and an administrator approves by ``digest``."""

    id: str
    generation: int
    action: str
    components: list[JSON]
    files: list[JSON]
    migrations: list[JSON]
    applied_migrations: list[Any]
    migration_targets: list[Any]
    impact: JSON
    maintenance: bool
    backup_required: bool
    recovery: str
    release: str
    release_digest: str
    artifacts: list[str]
    app_ids: list[str]
    created_at: str
    backend: str
    presentation_changes: list[JSON]
    resources: list[JSON]
    digest: str


class PlanRequest(TypedDict):
    action: str
    artifacts: list[str]
    app_ids: list[str]


class PlanRecord(TypedDict, total=False):
    """``plans/<id>.json``: the request and state of a preview, then the prepared plan (#298)."""

    state: str  # preparing | ready | failed; records written before #298 have none (ready).
    request: PlanRequest
    generation: int
    created_at: str
    error: str | None
    details: Any
    plan: Plan
    inventory: JSON
    owned_files: JSON
    source_owned_digest: str
    previous_release: str
    previous_inventory: JSON
    previous_owned_files: JSON


class Operation(TypedDict, total=False):
    """``operations/<id>.json``: the durable journal of one approved operation."""

    id: str
    operation: str
    action: str
    state: str
    stage: str
    generation: int
    expected_generation: int
    plan: Plan
    plan_digest: str
    binding: Binding
    backup: str | None
    release: str
    previous_release: str
    error: str | None
    created_at: str
    updated_at: str
    traffic_released: bool
    events: list[Event]


class ActiveState(TypedDict):
    """``active.json``: the active release, its inventory and its owned-file snapshot."""

    release: str
    inventory: JSON
    owned_files: JSON
