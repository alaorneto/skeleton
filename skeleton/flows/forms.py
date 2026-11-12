"""Server-side validation of submitted form data against the declared fields (docs/flows.md).

The accepted fields are an allowlist: a name that is not declared is refused, never ignored.
Sensitive values are returned apart, so callers store them in the redacted columns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

from .spec import Field, Form
from .values import InvalidValue, check_bounds, encode, parse

MAX_COMMENT = 4000


def merged_fields(forms: Iterable[Form | None]) -> dict[str, Field]:
    """The fields accepted together (step form plus transition form); the check keeps them coherent."""
    fields: dict[str, Field] = {}
    for form in forms:
        if form is not None:
            for item in form.fields:
                fields.setdefault(item.name, item)
    return fields


def _active_member(value: UUID) -> bool:
    from skeleton.tenancy.models import Membership

    # Tenant-filtered manager: a person of another organization is simply not found.
    return Membership.objects.filter(
        user_id=value, is_active=True, user__is_active=True, user__deleted_at__isnull=True
    ).exists()


def clean(forms: Iterable[Form | None], data: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validated ``(values, sensitive_values)`` as stored JSON scalars; raises ``ValidationError`` by field."""
    fields = merged_fields(forms)
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ValidationError({"data": gettext("Envie os campos do formulário.")})
    errors: dict[str, str] = {}
    for name in data:
        if not isinstance(name, str) or name not in fields:
            errors[str(name)[:64]] = gettext("Campo não permitido.")
    values: dict[str, Any] = {}
    sensitive: dict[str, Any] = {}
    for name, field in fields.items():
        if name in errors:
            continue
        try:
            value = parse(field, data.get(name))
            check_bounds(field, value)
            if value is None or (field.type == "bool" and value is False and name not in data):
                if field.required:
                    raise InvalidValue(gettext("Campo obrigatório."))
                if name not in data:
                    continue  # Not informed: keep whatever the instance already has.
                value = None
            if field.type == "user" and value is not None and not _active_member(value):
                raise InvalidValue(gettext("Pessoa sem vínculo ativo nesta organização."))
        except InvalidValue as error:
            errors[name] = str(error)
            continue
        (sensitive if field.sensitive else values)[name] = encode(value)
    if errors:
        raise ValidationError(errors)
    return values, sensitive


def clean_comment(comment: Any) -> str:
    if comment is None:
        return ""
    if not isinstance(comment, str) or len(comment) > MAX_COMMENT:
        raise ValidationError(
            {"comment": gettext("Use um texto de até %(MAX_COMMENT)s caracteres.") % {"MAX_COMMENT": MAX_COMMENT}}
        )
    return comment.strip()
