"""CSV dialect of transfers (#182): BOM, separator, formats and formula neutralization."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from skeleton.transfer import csv_format


def test_file_opens_in_ptbr_spreadsheets():
    data = csv_format.render(("sysid", "title"), [(UUID(int=1), 'Ação; "citação"')])
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert text.splitlines()[0] == "sysid;title"
    assert csv_format.read(data) == [["sysid", "title"], [str(UUID(int=1)), 'Ação; "citação"']]
    assert "\r\n" in text


def test_typed_values_have_stable_formats():
    row = (
        None,
        True,
        False,
        -5,
        Decimal("1234.50"),
        1.5,
        date(2026, 11, 12),
        datetime(2026, 11, 12, 10, 30, tzinfo=UTC),
        {"b": 1, "a": "x"},
    )
    assert csv_format.read(csv_format.render(range(len(row)), [row]))[1] == [
        "",
        "true",
        "false",
        "-5",
        "1234.50",
        "1.5",
        "2026-11-12",
        "2026-11-12T10:30:00+00:00",
        '{"a": "x", "b": 1}',
    ]


@pytest.mark.parametrize("text", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx", '=HYPERLINK("x")'])
def test_formula_cells_are_neutralized(text):
    assert csv_format.cell(text) == "'" + text
    assert csv_format.read(csv_format.render(("v",), [(text,)]))[1] == ["'" + text]


def test_plain_text_is_kept():
    for text in ("Contrato", "a=b", "x-y", "", "'quoted"):
        assert csv_format.cell(text) == text
