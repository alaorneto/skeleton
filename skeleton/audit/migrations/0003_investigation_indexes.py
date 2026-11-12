"""Audit investigation indexes (#184).

Built CONCURRENTLY so a large production audit table keeps accepting writes; the migration is
therefore not atomic. A failed build leaves an INVALID index: drop it and migrate again
(docs/audit-soft-delete.md).
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("sk_audit", "0002_record_infrastructure")]

    operations = [
        AddIndexConcurrently(
            model_name="auditevent",
            index=models.Index(fields=("organization_id", "actor_sysid", "created_at"), name="sk_audit_org_actor"),
        ),
        AddIndexConcurrently(
            model_name="auditevent",
            index=models.Index(fields=("organization_id", "request_id"), name="sk_audit_org_request"),
        ),
    ]
