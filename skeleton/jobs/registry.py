"""Job types are declared in code; no import path ever comes from stored data."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

PAYLOAD_TYPES = frozenset({"uuid", "int", "bool", "str"})
RUN_AS = frozenset({"system", "actor"})
SCOPES = frozenset({"organization", "global"})


@dataclass(frozen=True)
class JobType:
    """A registered kind of job.

    ``payload_schema`` maps each key to ``uuid``, ``int``, ``bool`` or ``str``
    (at most 200 characters). Every declared key is required and no other key is
    accepted. ``run_as="system"`` runs in ``system_context`` of the job
    organization; ``run_as="actor"`` re-enters ``tenant_context`` as the actor,
    which revalidates the membership. ``scope="global"`` jobs have no organization.
    """

    code: str
    handler: Callable[..., Any]
    payload_schema: Mapping[str, str] = field(default_factory=dict)
    run_as: str = "system"
    max_attempts: int = 5
    backoff: float = 30.0
    timeout_s: int = 300
    queue: str = "default"
    scope: str = "organization"
    owner_app: str = ""

    def enqueue(self, **kwargs: Any):
        """``django.tasks``-like entry point: ``JobType.enqueue(...)`` returns a receipt with ``id``."""
        from .services import enqueue

        return enqueue(self.code, **kwargs)


_registry: dict[str, JobType] = {}
_duplicates: list[str] = []


def register_job_type(job_type: JobType) -> JobType:
    """Register in ``AppConfig.ready()``; a second registration of the code is reported by a check."""
    existing = _registry.get(job_type.code)
    if existing is not None and existing != job_type:
        _duplicates.append(job_type.code)
        return existing
    _registry[job_type.code] = job_type
    return job_type


def get_job_type(code: str) -> JobType:
    return _registry[code]


def all_job_types() -> tuple[JobType, ...]:
    return tuple(_registry[code] for code in sorted(_registry))


def job_type_problems() -> list[str]:
    problems = [f"{code}: duplicate job type registration" for code in sorted(set(_duplicates))]
    for job_type in all_job_types():
        if not callable(job_type.handler):
            problems.append(f"{job_type.code}: handler is not callable")
        if job_type.run_as not in RUN_AS:
            problems.append(f"{job_type.code}: run_as must be system or actor")
        if job_type.scope not in SCOPES:
            problems.append(f"{job_type.code}: scope must be organization or global")
        if job_type.scope == "global" and job_type.run_as == "actor":
            problems.append(f"{job_type.code}: actor jobs need an organization")
        if any(kind not in PAYLOAD_TYPES for kind in job_type.payload_schema.values()):
            problems.append(f"{job_type.code}: payload types must be uuid, int, bool or str")
        if not 1 <= job_type.max_attempts <= 100 or job_type.backoff <= 0 or job_type.timeout_s <= 0:
            problems.append(f"{job_type.code}: invalid retry or timeout settings")
    return problems
