"""Every list of the core is readable on a phone (#777).

Below 640 px a ``table-stack`` table turns each row into a card and shows the column of each cell
from its ``data-label``. A table of the core either stacks, with a label on every cell, or is
listed here with the reason it keeps the horizontal scroll inside its card.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "skeleton", ROOT / "examples"]
SCROLL_ONLY = {
    # Dense operational tables: many narrow columns that are compared across rows.
    "skeleton/web/templates/skeleton/web/components/audit-table.html": "auditoria: colunas comparadas linha a linha",
    "skeleton/web/templates/skeleton/web/platform/jobs.html": "jobs: nove colunas operacionais",
    "skeleton/web/templates/skeleton/web/platform/updates/plan-details.html": "plano de atualização: tabelas técnicas",
    "skeleton/contrib/ledger/templates/skeleton_ledger/report.html": "relatórios contábeis: colunas de valores",
    # Columns inside {% if %}: the cells do not line up with the headings statically.
    "skeleton/contrib/ledger/templates/skeleton_ledger/accounts.html": "colunas condicionais",
    "skeleton/files/templates/skeleton/files/attachments_panel.html": "colunas condicionais",
    "skeleton/web/templates/skeleton/web/billing.html": "colunas condicionais",
    "skeleton/web/templates/skeleton/web/site_domains.html": "colunas condicionais",
    # Not a data table: allauth's slot wrapper.
    "skeleton/web/templates/allauth/elements/table.html": "elemento do allauth",
}
TABLE = re.compile(r'<div class="table-scroll(?P<cls>[^"]*)"[^>]*><table[^>]*>(?P<body>.*?)</table>', re.S)


def _tables():
    for source in SOURCES:
        for path in sorted(source.rglob("*.html")):
            relative = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            for match in TABLE.finditer(text):
                yield relative, match


def test_every_table_stacks_or_has_a_reason_to_scroll():
    problems = []
    for relative, match in _tables():
        if relative in SCROLL_ONLY:
            continue
        if "table-stack" not in match.group("cls"):
            problems.append(f"{relative}: table without table-stack")
            continue
        tbody = match.group("body").split("<tbody>", 1)[-1]
        for cell in re.finditer(r"<td\b(?P<attrs>[^>]*)>", tbody):
            if "data-label" not in cell.group("attrs") and "empty-state" not in cell.group("attrs"):
                problems.append(f"{relative}: <td{cell.group('attrs')}> without data-label")
                break
    assert problems == []


def test_the_allowlist_has_no_stale_entries():
    files = {relative for relative, _ in _tables()} | {
        "skeleton/web/templates/allauth/elements/table.html",
    }
    assert set(SCROLL_ONLY) <= files
