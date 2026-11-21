"""Writes of the instance appearance (#592): only the platform administrator, always with CAS.

Every function is keyword-only and runs in ``transaction.atomic()``; the process cache is reset
after the commit. ``actor=None`` is reserved for the console (``manage.py appearance``), which
opens its own ``system_context`` with a reason; HTTP views always pass ``request.user``.
Closed field lists: anything else is refused with "Campo não permitido.".
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

from .snapshot import reset_appearance_cache

BRAND_FIELDS = {"product_name": 60, "footer_text": 160}


def _require_platform(actor: Any) -> None:
    from skeleton.accounts.platform import is_platform_admin

    if actor is not None and not is_platform_admin(actor):
        raise PermissionDenied("Platform administration required.")


def _check_revision(expected_revision: Any) -> None:
    if type(expected_revision) is not int:
        raise ConcurrentUpdate("A revision is required.")


def current_brand() -> Any:
    from .models import InstanceBrand

    return InstanceBrand.objects.filter(slot="instance").first()


def revision_of(record: Any) -> int:
    return record.revision if record else 0


def _refuse_unknown(data: Any, allowed: Any) -> None:
    if not isinstance(data, dict):
        raise ValidationError(gettext("Dados inválidos."))
    unknown = set(data) - set(allowed)
    if unknown:
        raise ValidationError({name: gettext("Campo não permitido.") for name in sorted(unknown)})


def _line(name: str, value: Any, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError({name: gettext("Use um texto.")})
    value = value.strip()
    if any(char in value for char in "\n\r\x00"):
        raise ValidationError({name: gettext("Use uma única linha, sem quebras.")})
    if len(value) > limit:
        raise ValidationError({name: gettext("Use até %(limit)d caracteres.") % {"limit": limit}})
    if required and not value:
        raise ValidationError({name: gettext("Informe um nome.")})
    return value


def _locked_brand(expected_revision: Any) -> Any:
    """The brand row under ``select_for_update`` after the CAS check (``None`` with revision 0)."""
    from .models import InstanceBrand

    _check_revision(expected_revision)
    record = InstanceBrand.objects.select_for_update().filter(slot="instance").first()
    if revision_of(record) != expected_revision:
        raise ConcurrentUpdate("Instance brand changed.")
    return record


def _create_brand(**values: Any) -> Any:
    """Create the single row; a concurrent creator wins and this caller gets ``ConcurrentUpdate``."""
    from .models import InstanceBrand

    record = InstanceBrand(**values)
    try:
        with transaction.atomic():
            record.save()
    except IntegrityError:
        raise ConcurrentUpdate("Instance brand created concurrently.") from None
    return record


def update_brand(*, actor: Any, data: dict[str, Any], expected_revision: int) -> Any:
    """Change the product name and the footer text; equal values write nothing."""
    _require_platform(actor)
    _refuse_unknown(data, BRAND_FIELDS)
    values = {name: _line(name, value, BRAND_FIELDS[name]) for name, value in data.items()}
    with transaction.atomic():
        record = _locked_brand(expected_revision)
        if record is None:
            if not any(values.values()):
                return None
            record = _create_brand(**values)
        else:
            changed = {name for name, value in values.items() if getattr(record, name) != value}
            for name in changed:
                setattr(record, name, values[name])
            if not changed:
                return record
            record.save(update_fields=changed)
        transaction.on_commit(reset_appearance_cache)
    return record
