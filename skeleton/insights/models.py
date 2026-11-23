"""Management figures of the instance (#710): one summary per organization and daily metrics.

Both are operational global tables: written only by the insights jobs, never audited per row,
never exposed in the gateway, OpenAPI or CSV; ``InstanceMetric`` rows are deleted only by the
retention of ``insights.daily``. ``OrganizationSnapshot.updated_at`` changes only when a value
changes: it is the cursor of the platform API (#711).
"""

from django.db import models

from skeleton.common.models import OperationalRecord


class OrganizationSnapshot(OperationalRecord):
    subject_organization = models.OneToOneField("sk_tenancy.Organization", on_delete=models.PROTECT, related_name="+")
    name = models.CharField(max_length=200)
    slug = models.CharField(max_length=100)
    is_active = models.BooleanField()
    organization_created_at = models.DateTimeField()
    members_active = models.PositiveIntegerField(default=0)
    members_limit = models.PositiveIntegerField(null=True, blank=True)
    external_people = models.PositiveIntegerField(default=0)
    service_accounts = models.PositiveIntegerField(default=0)
    apps_effective = models.JSONField(default=list, blank=True)
    bundle_code = models.CharField(max_length=64, blank=True, default="")
    storage_bytes_used = models.BigIntegerField(default=0)
    storage_quota_bytes = models.BigIntegerField(null=True, blank=True)
    storage_location_code = models.CharField(max_length=64, blank=True, default="")
    billing = models.JSONField(default=dict, blank=True)
    # Per UTC day (today and yesterday): invoiced, paid and refunded by currency, and app metrics.
    daily = models.JSONField(default=dict, blank=True)
    app_metrics = models.JSONField(default=dict, blank=True)
    refreshed_at = models.DateTimeField()
    data_version = models.PositiveIntegerField(default=1)
    immutable_fields = ("subject_organization_id",)

    def __str__(self):
        return f"{self.slug} v{self.data_version}"


class InstanceMetric(OperationalRecord):
    day = models.DateField()
    code = models.CharField(max_length=120)
    currency = models.CharField(max_length=3, blank=True, default="")
    value = models.BigIntegerField()
    immutable_fields = ("day", "code", "currency")

    class Meta(OperationalRecord.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=("day", "code", "currency"),
                name="sk_insights_instancemetric_unique",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return f"{self.day} {self.code} {self.currency} {self.value}"
