"""CSV format of exports and imports (#182, #183): UTF-8 with BOM, ``;``, ISO 8601, decimal point.

Opened by Excel in pt-BR without the import wizard, and read back by the import unchanged.
A text cell starting with ``=``, ``+``, ``-``, ``@``, tab or carriage return gets a leading
apostrophe, so a spreadsheet never evaluates it as a formula (CSV injection); numbers, dates
and booleans are written as values and never prefixed. The reader accepts ``;`` or ``,``,
detected from the header, and strips the apostrophe the writer added.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

BOM = "﻿"
DELIMITER = ";"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def cell(value: Any) -> str:
    """One value as CSV text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal | int | float):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def write_csv(header: Iterable[str], rows: Iterable[Iterable[Any]]) -> bytes:
    buffer = io.StringIO()
    buffer.write(BOM)
    writer = csv.writer(buffer, delimiter=DELIMITER, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(list(header))
    for row in rows:
        writer.writerow([cell(value) for value in row])
    return buffer.getvalue().encode("utf-8")


class CsvFormatError(ValueError):
    pass


def read_csv(content: bytes) -> tuple[list[str], Iterator[list[str]]]:
    """Header and rows of an uploaded CSV; ``;`` or ``,`` chosen by the header line."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvFormatError("encoding") from None
    first = text.split("\n", 1)[0]
    delimiter = ";" if first.count(";") >= first.count(",") else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise CsvFormatError("empty") from None
    header = [name.strip() for name in header]
    if not header or any(not name for name in header) or len(set(header)) != len(header):
        raise CsvFormatError("header")

    def rows() -> Iterator[list[str]]:
        for row in reader:
            if not any(value.strip() for value in row):
                continue
            yield [
                value[1:] if value.startswith("'") and value[1:].startswith(FORMULA_START) else value for value in row
            ]

    return header, rows()
