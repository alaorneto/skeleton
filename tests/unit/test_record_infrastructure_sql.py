"""The SQL that InstallRecordInfrastructure emits for every core table stays byte for byte the same (#316).

check_skeleton_schema compares the installed functions with this text, so a change of one
character would fail every existing installation. ``SKELETON_UPDATE_SQL_SNAPSHOT=1`` rewrites the
snapshot (merging with the stored one) instead of comparing.
"""

import hashlib
import json
import os
from pathlib import Path

from django.apps import apps
from django.db import connection

SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "record_infrastructure_sql.json"
REQUIRED = {
    "sk_accounts.user",  # Global.
    "sk_access.role",  # Organization.
    "sk_tenancy.membership",  # Self-discovery.
    "sk_audit.auditevent",  # Append-only.
    "sk_jobs.job",  # Operational with tenant_column.
    "sk_integrations.webhookdelivery",  # Operational of an organization.
}


def _digests():
    from skeleton.system.checks import _expected_sql, _frozen_operations

    operations, _, _ = _frozen_operations(from_migrations=True)
    digests = {}
    for label, operation in operations.items():
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        sql = "\n".join(str(statement) for statement in _expected_sql(operation, model, connection))
        digests[label] = hashlib.sha256(sql.encode()).hexdigest()
    return digests


def test_emitted_sql_matches_the_snapshot():
    current = _digests()
    if os.environ.get("SKELETON_UPDATE_SQL_SNAPSHOT") == "1":
        stored = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.exists() else {}
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps({**stored, **current}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    stored = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert REQUIRED <= set(stored)
    compared = {label: digest for label, digest in current.items() if label in stored}
    assert compared, "no table of the snapshot is present"
    assert compared == {label: stored[label] for label in compared}
