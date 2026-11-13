"""Job idempotency keys are unique per organization, not across tenants (#241).

The new partial constraints are created before the old one is dropped, so the queue is never
without one. Existing rows already satisfy them (the old constraint was stricter).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("sk_jobs", "0002_worker_heartbeat")]

    operations = [
        migrations.AddConstraint(
            model_name="job",
            constraint=models.UniqueConstraint(
                condition=models.Q(("idempotency_key__isnull", False), ("job_organization__isnull", False)),
                fields=("job_type", "job_organization", "idempotency_key"),
                name="sk_jobs_job_idem_org",
                violation_error_code="unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.UniqueConstraint(
                condition=models.Q(("idempotency_key__isnull", False), ("job_organization__isnull", True)),
                fields=("job_type", "idempotency_key"),
                name="sk_jobs_job_idem_global",
                violation_error_code="unique",
            ),
        ),
        migrations.RemoveConstraint(model_name="job", name="sk_jobs_job_idempotency"),
    ]
