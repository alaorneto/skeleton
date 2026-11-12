"""Typed values of form fields: parsing of input, JSON storage and decoding for comparisons.

Stored form data is plain JSON: decimals, dates and UUIDs travel as text and are decoded with
the declared field type before any comparison, so ``"900" < "1000"`` never compares as text.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from django.utils.translation import gettext

from .spec import Field

MAX_TEXT = 20000
MAX_STR = 500
ORDERED_TYPES = ("int", "decimal", "date", "datetime", "str", "text", "choice")


class InvalidValue(ValueError):
    """A value that does not fit the declared field; the message is safe to show."""


def _decimal(raw: Any) -> Decimal:
    if isinstance(raw, bool) or isinstance(raw, float):
        raise InvalidValue(gettext("Use um número decimal em texto."))
    try:
        value = Decimal(str(raw).strip()) if not isinstance(raw, Decimal) else raw
    except (InvalidOperation, ValueError):
        raise InvalidValue(gettext("Número decimal inválido.")) from None
    if not value.is_finite():
        raise InvalidValue(gettext("Número decimal inválido."))
    return value


def _datetime(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw.strip())
        except ValueError:
            raise InvalidValue(gettext("Use data e hora no formato ISO 8601.")) from None
    else:
        raise InvalidValue(gettext("Use data e hora no formato ISO 8601."))
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidValue(gettext("Informe o fuso da data e hora."))
    return value.astimezone(UTC)


def _date(raw: Any) -> date:
    if isinstance(raw, datetime):
        raise InvalidValue(gettext("Use uma data, sem hora."))
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            pass
    raise InvalidValue(gettext("Use uma data no formato AAAA-MM-DD."))


def parse(field: Field, raw: Any) -> Any:
    """The typed value of ``raw`` for ``field``; ``None`` and blank text mean "not informed"."""
    if raw is None or (isinstance(raw, str) and not raw.strip() and field.type != "bool"):
        return None
    kind = field.type
    if kind in ("str", "text", "choice"):
        if not isinstance(raw, str):
            raise InvalidValue(gettext("Use um texto."))
        limit = field.max_length or (MAX_TEXT if kind == "text" else MAX_STR)
        value = raw if kind == "text" else raw.strip()
        if len(value) > limit:
            raise InvalidValue(gettext("Use até %(limit)s caracteres.") % {"limit": limit})
        if kind == "choice" and value not in field.choices:
            raise InvalidValue(gettext("Escolha uma das opções."))
        return value
    if kind == "int":
        if isinstance(raw, bool):
            raise InvalidValue(gettext("Use um número inteiro."))
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            if text.lstrip("+-").isdigit() and text.isascii() and len(text) <= 20:
                return int(text)
        raise InvalidValue(gettext("Use um número inteiro."))
    if kind == "decimal":
        return _decimal(raw)
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "on", "1", "false", "off", "0", ""):
            return raw.strip().lower() in ("true", "on", "1")
        raise InvalidValue(gettext("Use verdadeiro ou falso."))
    if kind == "date":
        return _date(raw)
    if kind == "datetime":
        return _datetime(raw)
    if kind == "user":
        try:
            return raw if isinstance(raw, UUID) else UUID(str(raw).strip())
        except (ValueError, AttributeError):
            raise InvalidValue(gettext("Identificador de pessoa inválido.")) from None
    raise InvalidValue(gettext("Tipo de campo desconhecido."))


def check_bounds(field: Field, value: Any) -> None:
    if value is None or field.type not in ("int", "decimal"):
        return
    number = Decimal(value) if not isinstance(value, Decimal) else value
    if field.minimum != "" and number < Decimal(field.minimum):
        raise InvalidValue(gettext("O mínimo é %(minimum)s.") % {"minimum": field.minimum})
    if field.maximum != "" and number > Decimal(field.maximum):
        raise InvalidValue(gettext("O máximo é %(maximum)s.") % {"maximum": field.maximum})


def encode(value: Any) -> Any:
    """The JSON scalar stored for a typed value."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise InvalidValue(gettext("Valor não suportado."))


def decode(field: Field, stored: Any) -> Any:
    """The typed value of a stored scalar; a value that no longer fits decodes to ``None``."""
    try:
        return parse(field, stored)
    except InvalidValue:
        return None
