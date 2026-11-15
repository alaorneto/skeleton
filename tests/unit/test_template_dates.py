"""No core template prints a date raw: every date goes through |moment or an explicit |date (#468)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Email bodies keep their own formats; maintenance.html renders without the full Django setup.
EXCEPTIONS = (
    "skeleton/web/templates/skeleton/maintenance.html",
    "notifications/templates/notifications/",
    "tenancy/templates/skeleton/tenancy/invitation/",
    "skeleton/email/",
)
VARIABLE = re.compile(r"\{\{\s*([^}]*?)\s*\}\}")
DATE_NAME = re.compile(r"^[A-Za-z_][\w.]*?(?:_at|last_login|run_after|\.at)\b")


def raw_dates(text: str) -> list[int]:
    lines = []
    for match in VARIABLE.finditer(text):
        expression = match.group(1)
        name = expression.split("|", 1)[0].strip()
        if not DATE_NAME.match(name) or name.endswith(".isoformat"):
            continue
        if "|moment" in expression or "|date:" in expression:
            continue
        lines.append(text.count("\n", 0, match.start()) + 1)
    return lines


def test_no_core_template_prints_a_raw_date():
    problems = []
    for template in sorted((ROOT / "skeleton").rglob("templates/**/*.html")):
        relative = template.relative_to(ROOT).as_posix()
        if any(part in relative for part in EXCEPTIONS):
            continue
        problems += [f"{relative}:{line}" for line in raw_dates(template.read_text(encoding="utf-8"))]
    assert problems == [], "use {{ value|moment }}: " + ", ".join(problems)


def test_the_guard_sees_raw_dates():
    assert raw_dates("{{ row.created_at }}\n{{ job.finished_at|default:'—' }}") == [1, 2]
    assert raw_dates("{{ row.created_at|moment }} {{ x.due_at|date:'c' }} {{ job.updated_at.isoformat }}") == []
