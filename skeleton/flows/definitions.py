"""Flows authored as data by an organization (#414): drafts, validation and publication.

Only who administers flows (the administrator or ``flows.manage``) reads, writes and publishes
drafts. A draft holds the JSON shape of ``FlowSpec.to_json`` without the version; it may
reference only what the code registered (service handlers, conditions, assignment rules, the
owning app's exposed tables and its sequences and event types), checked by the same validator as
the flows declared in code. Nothing a client sends is ever executed: conditions are data trees or
names of registered callables.

Publishing locks the draft, checks the revision, validates and writes an immutable
``FlowDefinition`` for the next version of the key; new instances of the key start from it.
A key declared in code cannot be authored as data, and a published version never changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

MAX_SPECIFICATION_BYTES = 200_000
DRAFT_VERSION = 1  # Placeholder while validating: publication sets the real one.


@dataclass(frozen=True)
class DraftInfo:
    sysid: UUID
    revision: int
    key: str
    label: str
    app_label: str
    specification: dict[str, Any]
    published_version: int | None
    published_at: datetime | None
    problems: tuple[str, ...]


def _require_admin(user: Any) -> None:
    from . import policies

    if not policies.is_flow_admin(user):
        raise PermissionDenied("Flow administration required.")


def _spec_of(specification: Any, version: int) -> Any:
    from .spec import FlowSpec

    return FlowSpec.from_json({**specification, "version": version})


def problems_of(specification: Any, *, key: str) -> list[str]:
    """Why a draft cannot be published (empty when it can)."""
    from . import policies
    from .registry import get_flow, spec_problems

    if not isinstance(specification, dict):
        return ["specification: must be an object"]
    if specification.get("key") != key:
        return ["specification: key must be the draft's key"]
    try:
        get_flow(key)
    except KeyError:
        pass
    else:
        return [f"{key}: declared in code; data flows use their own keys"]
    try:
        spec = _spec_of(specification, DRAFT_VERSION)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        return [f"specification: malformed ({type(error).__name__}: {error})"]
    problems = spec_problems(spec)
    if not problems:
        from .errors import FlowUnavailable

        try:
            policies.require_flow_app(spec.owner_app)
        except FlowUnavailable:
            problems.append(f"app {spec.owner_app!r} is not enabled in this organization")
    return problems


def _info(draft: Any) -> DraftInfo:
    return DraftInfo(
        sysid=draft.pk,
        revision=draft.revision,
        key=draft.key,
        label=draft.label,
        app_label=draft.app_label,
        specification=draft.specification,
        published_version=draft.published_version,
        published_at=draft.published_at,
        problems=tuple(problems_of(draft.specification, key=draft.key)),
    )


def drafts(user: Any) -> tuple[DraftInfo, ...]:
    from .models import FlowDraft

    _require_admin(user)
    return tuple(_info(row) for row in FlowDraft.objects.order_by("key", "sysid"))


def draft(user: Any, draft_id: Any) -> DraftInfo:
    from .models import FlowDraft

    _require_admin(user)
    row = FlowDraft.objects.filter(pk=draft_id).first()
    if row is None:
        raise FlowDraft.DoesNotExist("Flow draft unavailable.")
    return _info(row)


def _clean(specification: Any) -> dict[str, Any]:
    if not isinstance(specification, dict):
        raise ValidationError({"specification": gettext("Envie a especificação como objeto JSON.")})
    if len(json.dumps(specification)) > MAX_SPECIFICATION_BYTES:
        raise ValidationError({"specification": gettext("Especificação grande demais.")})
    cleaned = dict(specification)
    cleaned.pop("version", None)  # Set on publication; a draft never chooses its version.
    return cleaned


def save_draft(user: Any, *, specification: Any, draft_id: Any = None, expected_revision: Any = None) -> DraftInfo:
    """Create a draft (``draft_id=None``) or replace the specification of one, with CAS."""
    from .models import FlowDraft
    from .registry import get_flow
    from .validation import FLOW_KEY

    _require_admin(user)
    cleaned = _clean(specification)
    key, label, app_label = cleaned.get("key"), cleaned.get("label", ""), cleaned.get("owner_app", "")
    if not isinstance(key, str) or not FLOW_KEY.fullmatch(key) or len(key) > 100:
        raise ValidationError({"specification": gettext("Chave do fluxo inválida (app_label.nome).")})
    if not isinstance(label, str) or not isinstance(app_label, str) or len(label) > 200 or len(app_label) > 100:
        raise ValidationError({"specification": gettext("Rótulo ou app inválidos.")})
    try:
        get_flow(key)
    except KeyError:
        pass
    else:
        raise ValidationError({"specification": gettext("Essa chave é de um fluxo declarado em código.")})
    from skeleton.tenancy.context import current_context

    with transaction.atomic(using=current_context().using):
        if draft_id is None:
            row = FlowDraft(organization_id=current_context().organization_id, key=key)
        else:
            row = FlowDraft.objects.select_for_update().filter(pk=draft_id).first()
            if row is None:
                raise FlowDraft.DoesNotExist("Flow draft unavailable.")
            if type(expected_revision) is not int or row.revision != expected_revision:
                raise ConcurrentUpdate("The flow draft changed.")
            if row.key != key:
                raise ValidationError({"specification": gettext("A chave de um rascunho não muda.")})
        row.label, row.app_label, row.specification = label[:200], app_label[:100], cleaned
        try:
            with transaction.atomic(using=current_context().using):
                row.save()
        except IntegrityError:
            raise ValidationError({"specification": gettext("Já existe um rascunho com essa chave.")}) from None
    return _info(row)


def publish_draft(user: Any, *, draft_id: Any, expected_revision: Any) -> DraftInfo:
    """Validate and publish the draft as the next version of its key."""
    from skeleton.tenancy.context import current_context

    from .models import FlowDefinition, FlowDraft
    from .services import ensure_definition

    _require_admin(user)
    with transaction.atomic(using=current_context().using):
        row = FlowDraft.objects.select_for_update().filter(pk=draft_id).first()
        if row is None:
            raise FlowDraft.DoesNotExist("Flow draft unavailable.")
        if type(expected_revision) is not int or row.revision != expected_revision:
            raise ConcurrentUpdate("The flow draft changed.")
        problems = problems_of(row.specification, key=row.key)
        if problems:
            raise ValidationError({"specification": problems})
        latest = FlowDefinition.objects.filter(key=row.key).aggregate(latest=Max("version"))["latest"] or 0
        version = latest + 1
        ensure_definition(_spec_of(row.specification, version))
        row.published_version = version
        row.published_at = timezone.now()
        row.save(update_fields={"published_version", "published_at"})
    return _info(row)


def published_data_spec(flow_key: Any) -> Any:
    """The published version of a data flow of the current organization, or ``None``."""
    from .engine import specification_of
    from .models import FlowDefinition, FlowDraft

    if not isinstance(flow_key, str):
        return None
    row = FlowDraft.objects.filter(key=flow_key, published_version__isnull=False).first()
    if row is None:
        return None
    definition = FlowDefinition.objects.filter(key=flow_key, version=row.published_version).first()
    return specification_of(definition) if definition is not None else None
