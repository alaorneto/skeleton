"""The CSV dialect of transfers: what opens cleanly in a pt-BR spreadsheet and is safe to open.

- UTF-8 with BOM, so Excel reads accents correctly;
- ``;`` as the separator, since ``,`` is the decimal mark of pt-BR spreadsheets;
- dates and datetimes in ISO 8601, decimals with a dot, booleans as ``true``/``false``;
- a text cell that a spreadsheet could run as a formula (it starts with ``=``, ``+``, ``-``,
  ``@``, tab or carriage return) gets a leading ``'``. Typed numbers are not text and are
  written as they are: ``-5`` stays a number.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Iterator
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

BOM = "﻿"
DELIMITER = ";"
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
NEUTRALIZER = "'"


def neutralize(text: str) -> str:
    """Prefix a text that a spreadsheet would read as a formula."""
    return NEUTRALIZER + text if text.startswith(FORMULA_PREFIXES) else text


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict | list):
        return neutralize(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return neutralize(str(value))


class _Buffer:
    def __init__(self) -> None:
        self.value = ""

    def write(self, text: str) -> None:
        self.value += text


def encode_rows(header: Iterable[str], rows: Iterable[Iterable[Any]]) -> Iterator[bytes]:
    """The file as UTF-8 chunks, one per row, starting with the BOM and the header."""
    buffer = _Buffer()
    writer = csv.writer(buffer, delimiter=DELIMITER, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(list(header))
    yield (BOM + buffer.value).encode("utf-8")
    for row in rows:
        buffer.value = ""
        writer.writerow([cell(value) for value in row])
        yield buffer.value.encode("utf-8")


def render(header: Iterable[str], rows: Iterable[Iterable[Any]]) -> bytes:
    """The whole file in memory; for tests and small files."""
    return b"".join(encode_rows(header, rows))


def read(data: bytes) -> list[list[str]]:
    """Parse a file written by ``render`` (tests and the import of #183 use their own reader)."""
    text = data.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=DELIMITER))
