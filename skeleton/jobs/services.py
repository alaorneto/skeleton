"""Enqueue in the caller's transaction: a rolled back business write leaves no job."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.tenancy.context import current_context

from .registry import JobType, get_job_type

MAX_PAYLOAD_BYTES = 4096
MAX_TEXT = 200


@dataclass(frozen=True, slots=True)
class JobReceipt:
    """What a caller learns about an enqueued job: never the payload back."""

    id: UUID
    job_type: str
    state: str
    run_after: datetime
    created: bool

    @property
    def status(self) -> str:  # django.tasks-like alias
        return self.state


def validate_payload(job_type: JobType, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError({"payload": gettext("O payload deve ser um objeto.")})
    schema = job_type.payload_schema
    extra, missing = set(payload) - set(schema), set(schema) - set(payload)
    if extra or missing:
        raise ValidationError(
            {
                "payload": gettext("Chaves inválidas: extra=%(value1)s, ausentes=%(value3)s.")
                % {"value1": sorted(extra), "value3": sorted(missing)}
            }
        )
    clean: dict[str, Any] = {}
    for key, kind in schema.items():
        value = payload[key]
        if kind == "uuid":
            try:
                clean[key] = str(UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                raise ValidationError({"payload": gettext("%(key)s: UUID inválido.") % {"key": key}}) from None
        elif kind == "int":
            if type(value) is not int:
                raise ValidationError({"payload": gettext("%(key)s: inteiro obrigatório.") % {"key": key}})
            clean[key] = value
        elif kind == "bool":
            if type(value) is not bool:
                raise ValidationError({"payload": gettext("%(key)s: booleano obrigatório.") % {"key": key}})
            clean[key] = value
        else:
            if not isinstance(value, str) or len(value) > MAX_TEXT:
                raise ValidationError(
                    {
                        "payload": gettext("%(key)s: texto de até %(MAX_TEXT)s caracteres.")
                        % {"key": key, "MAX_TEXT": MAX_TEXT}
                    }
                )
            clean[key] = value
    if len(json.dumps(clean, separators=(",", ":"), sort_keys=True).encode()) > MAX_PAYLOAD_BYTES:
        raise ValidationError({"payload": gettext("Payload acima de 4 KB.")})
    return clean


def _organization_for(job_type: JobType, organization_id: Any) -> UUID | None:
    context = current_context()
    if job_type.scope == "global":
        if organization_id is not None:
            raise ValidationError({"organization_id": gettext("Jobs globais não têm organização.")})
        if context.organization_id is not None:
            raise ValidationError(
                {"organization_id": gettext("Jobs globais não são enfileirados de dentro de um tenant.")}
            )
        return None
    if organization_id is None:
        raise ValidationError({"organization_id": gettext("Organização obrigatória.")})
    organization_id = UUID(str(organization_id))
    if context.organization_id is not None and context.organization_id != organization_id:
        raise ValidationError({"organization_id": gettext("A organização deve ser o tenant atual.")})
    return organization_id


def enqueue(
    code: str,
    *,
    organization_id: Any = None,
    payload: dict[str, Any] | None = None,
    actor: Any = None,
    idempotency_key: str | None = None,
    run_after: datetime | None = None,
) -> JobReceipt:
    """Insert one job in the current transaction.

    A repeated ``idempotency_key`` for the same type returns the existing job
    (inserted in a savepoint: one row, never a bulk upsert).
    """
    from .models import Job

    try:
        job_type = get_job_type(code)
    except KeyError:
        raise ValidationError({"job_type": gettext("Tipo de job desconhecido.")}) from None
    values = validate_payload(job_type, payload if payload is not None else {})
    organization = _organization_for(job_type, organization_id)
    actor_id = UUID(str(getattr(actor, "pk", actor))) if actor is not None else None
    if job_type.run_as == "actor" and (actor_id is None or organization is None):
        raise ValidationError({"actor": gettext("Jobs executados como ator exigem ator e organização.")})
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200):
        raise ValidationError({"idempotency_key": gettext("Chave de 1 a 200 caracteres.")})
    if run_after is not None and timezone.is_naive(run_after):
        raise ValidationError({"run_after": gettext("Use data e hora com fuso.")})
    job = Job(
        job_type=job_type.code,
        queue=job_type.queue,
        job_organization_id=organization,
        actor_id=actor_id,
        payload=values,
        max_attempts=job_type.max_attempts,
        run_after=run_after or timezone.now(),
        idempotency_key=idempotency_key,
    )
    try:
        with transaction.atomic(using=current_context().using):
            job.save()
        created = True
    except IntegrityError:
        if idempotency_key is None:
            raise
        job = Job.all_objects.get(job_type=job_type.code, idempotency_key=idempotency_key)
        if job.job_organization_id != organization:
            # Never hand out another tenant's job identity through a shared key.
            raise ValidationError({"idempotency_key": gettext("Chave já usada.")}) from None
        created = False
    return JobReceipt(job.pk, job.job_type, job.state, job.run_after, created)
