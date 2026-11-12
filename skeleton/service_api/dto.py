"""DTO conventions, pages and safe conversions (docs/service-api.md, "Retorno").

DTOs are ``@dataclass(frozen=True, slots=True)`` with UUID, str, int, bool, Decimal, date,
aware UTC datetime, text enums, tuples and other DTOs. Lists are always a bounded ``Page``.
"""

import base64
import binascii
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from django.utils.translation import gettext

from .errors import InvalidInput

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25
# Largest row offset a page or cursor may address; deeper reads need narrower filters (#271).
MAX_OFFSET = 1_000_000
_CURSOR_PREFIX = "o:"


@dataclass(frozen=True, slots=True)
class Page[T]:
    """A bounded slice of results; ``next_cursor`` is opaque and ``None`` on the last page."""

    items: tuple[T, ...]
    next_cursor: str | None
    page_size: int

    def __post_init__(self) -> None:
        if type(self.items) is not tuple:
            raise TypeError("Page.items must be a tuple.")
        if type(self.page_size) is not int or not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"Page.page_size must be between 1 and {MAX_PAGE_SIZE}.")
        if len(self.items) > self.page_size:
            raise ValueError("Page.items exceeds page_size.")
        if self.next_cursor is not None and type(self.next_cursor) is not str:
            raise TypeError("Page.next_cursor must be a string or None.")


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"{_CURSOR_PREFIX}{offset}".encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    """Return the offset of an opaque cursor; anything malformed is ``InvalidInput``."""
    if cursor is None:
        return 0
    invalid = InvalidInput({"cursor": [gettext("Cursor inválido.")]})
    if type(cursor) is not str or not cursor or len(cursor) > 64:
        raise invalid
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise invalid from None
    digits = text.removeprefix(_CURSOR_PREFIX)
    if not text.startswith(_CURSOR_PREFIX) or not digits.isdigit() or not digits.isascii():
        raise invalid
    offset = int(digits)
    if offset > MAX_OFFSET:
        raise invalid
    return offset


def validate_page_size(page_size: object) -> int:
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise InvalidInput(
            {"page_size": [gettext("Use um inteiro entre 1 e %(MAX_PAGE_SIZE)s.") % {"MAX_PAGE_SIZE": MAX_PAGE_SIZE}]}
        )
    return page_size


def paginate[T](
    source: Sequence[Any] | Any,  # a Sequence or an ordered QuerySet
    *,
    convert: Callable[[Any], T],
    page_size: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> Page[T]:
    """Slice an already authorized, ordered source and convert each row to a DTO.

    Filter (``authorized_queryset``) before paginating: counting or slicing first would leak.
    A QuerySet must be ordered, otherwise consecutive pages could repeat or skip rows.
    """
    from django.db.models import QuerySet

    size = validate_page_size(page_size)
    offset = decode_cursor(cursor)
    if isinstance(source, QuerySet) and not source.ordered:
        raise TypeError("paginate() requires an ordered QuerySet.")
    rows = list(source[offset : offset + size + 1])
    items = tuple(convert(row) for row in rows[:size])
    return Page(items=items, next_cursor=encode_cursor(offset + size) if len(rows) > size else None, page_size=size)


def as_uuid(value: object, *, field: str = "id") -> UUID:
    """Accept a UUID or its canonical string; anything else is ``InvalidInput``."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and len(value) <= 64:
        try:
            return UUID(value)
        except ValueError:
            pass
    raise InvalidInput({field: [gettext("Identificador inválido.")]})


def as_utc(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC; a naive datetime is a programming error."""
    if not isinstance(value, datetime):
        raise TypeError("as_utc() requires a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Service DTOs require timezone-aware datetimes.")
    return value.astimezone(UTC)
