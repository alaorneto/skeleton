"""docs/checks.md lists every skeleton.E/W check of the code, and E001–E010 explain the fix (#257)."""

import re
from pathlib import Path

from django.db import models
from django.test.utils import isolate_apps

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "checks.md"
CHECK_ID = re.compile(r"skeleton\.[EW]\d{3}")


def _code_ids():
    found = set()
    for path in (ROOT / "skeleton").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        found |= set(CHECK_ID.findall(path.read_text(encoding="utf-8")))
    return found


def _table_ids():
    rows = [line for line in REFERENCE.read_text(encoding="utf-8").splitlines() if line.startswith("| `skeleton.")]
    return [CHECK_ID.search(row.split("|")[1]).group(0) for row in rows]


def test_every_check_id_in_code_is_in_the_reference_table():
    table = _table_ids()
    assert len(table) == len(set(table)), "duplicate rows in docs/checks.md"
    code = _code_ids()
    assert sorted(code - set(table)) == [], "checks missing from docs/checks.md"
    assert sorted(set(table) - code) == [], "rows of docs/checks.md without a check in the code"


def test_reference_table_is_in_numeric_order():
    table = _table_ids()
    assert table == sorted(table, key=lambda value: (int(value[-3:]), value[-4] == "W"))


def test_next_free_identifier_is_above_every_used_number():
    match = re.search(r"Próximo livre: (\d{3})", REFERENCE.read_text(encoding="utf-8"))
    assert match, "docs/checks.md must state the next free identifier"
    used = max(int(value[-3:]) for value in _code_ids())
    assert int(match.group(1)) == used + 1


def test_table_contract_checks_have_hints(monkeypatch, settings):
    from skeleton.common.models import OrganizationRecord
    from skeleton.system import registry
    from skeleton.system.checks import record_contract_checks

    tenant = "skeleton.tenancy.middleware.TenantContextMiddleware"
    settings.MIDDLEWARE = [item for item in settings.MIDDLEWARE if item != tenant]  # E009
    monkeypatch.setitem(settings.DATABASES["default"], "ATOMIC_REQUESTS", True)  # E010
    with isolate_apps("skeleton.system.apps.SystemConfig") as isolated:

        class PlainModel(models.Model):  # E001 (and E002)
            class Meta:
                app_label = "sk_system"

        class NoSpec(OrganizationRecord):  # E002
            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"

        class Broken(OrganizationRecord):  # E003-E008
            secret = models.CharField(max_length=10)
            peers = models.ManyToManyField("self")

            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"

        Broken._meta.get_field("organization").null = True  # E006
        monkeypatch.setitem(
            registry._registry,
            Broken._meta.label_lower,
            registry.TableSpec(
                model=Broken,
                description="x",
                read_fields=("missing",),  # E003
                write_fields=("owner",),  # E004
                expose_api=True,  # E005
            ),
        )
        errors = [error for error in record_contract_checks(apps_registry=isolated) if error.id < "skeleton.E011"]
    assert {error.id for error in errors} == {f"skeleton.E{number:03d}" for number in range(1, 11)}
    for error in errors:
        assert error.hint and "docs/checks.md" in error.hint, error.id
