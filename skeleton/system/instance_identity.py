"""The identity of this instance (#708): a stable UUID, a name and a purpose.

One row (``InstanceIdentity``), created after ``migrate`` and never deleted. The identifier
leaves only through the platform API (#711), never through the anonymous ``GET /api/v1/server/``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

TTL = 60.0
_cache: dict[str, tuple[float, InstanceIdentityInfo]] = {}


@dataclass(frozen=True)
class InstanceIdentityInfo:
    instance_id: UUID
    name: str
    purpose: str
    revision: int


def ensure_instance_identity(*, using: str = "default") -> None:
    """Create the single row when it does not exist (``post_migrate``); never a second one."""
    from django.conf import settings

    from skeleton.tenancy.context import system_context

    from .models import InstanceIdentity

    with system_context(reason="migrate:instance-identity", using=using):
        if not InstanceIdentity.all_objects.using(using).exists():
            InstanceIdentity(name=str(getattr(settings, "SKELETON_PRODUCT_NAME", "") or "")[:200]).save(using=using)


def _info(row: Any) -> InstanceIdentityInfo:
    return InstanceIdentityInfo(row.instance_id, row.name, row.purpose, row.revision)


def instance_identity() -> InstanceIdentityInfo:
    """The identity of this instance, cached per process for 60 s."""
    from .models import InstanceIdentity

    hit = _cache.get("identity")
    now = time.monotonic()
    if hit is not None and now - hit[0] < TTL:
        return hit[1]
    row = InstanceIdentity.objects.first()
    if row is None:
        ensure_instance_identity()
        row = InstanceIdentity.objects.get()
    info = _info(row)
    _cache["identity"] = (now, info)
    return info


def forget_instance_identity() -> None:
    _cache.clear()


@transaction.atomic
def update_instance_identity(*, actor: Any, name: str, purpose: str, expected_revision: int) -> InstanceIdentityInfo:
    """Change the name and the purpose with CAS; only the platform administrator."""
    from skeleton.accounts.platform import require_platform_context

    from .models import InstanceIdentity

    require_platform_context(actor)
    if not isinstance(name, str) or not isinstance(purpose, str):
        raise ValidationError({"name": gettext("Use um texto.")})
    name, purpose = name.strip(), purpose.strip()
    if len(name) > 200:
        raise ValidationError({"name": gettext("Use até 200 caracteres.")})
    if len(purpose) > 500:
        raise ValidationError({"purpose": gettext("Use até 500 caracteres.")})
    row = InstanceIdentity.objects.select_for_update().get()
    if type(expected_revision) is not int or row.revision != expected_revision:
        raise ConcurrentUpdate("Instance identity changed.")
    changed = {field for field, value in (("name", name), ("purpose", purpose)) if getattr(row, field) != value}
    if changed:
        row.name, row.purpose = name, purpose
        row.save(update_fields=changed)
    transaction.on_commit(forget_instance_identity)
    forget_instance_identity()
    return _info(row)
