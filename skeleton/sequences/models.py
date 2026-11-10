from django.db import models

from skeleton.common.models import OrganizationRecord


class SequenceCounter(OrganizationRecord):
    """The last number handed out for one sequence and period in one organization.

    Only ``skeleton.sequences.services.next_number`` changes it, under a row lock in the
    caller's transaction; it is never exposed for editing.
    """

    key = models.CharField(max_length=100)
    period = models.CharField(max_length=7, blank=True, default="")
    last_value = models.PositiveBigIntegerField(default=0)
    immutable_fields = ("key", "period")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "key", "period"),
                name="sk_sequences_counter_period",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return f"{self.key} {self.period}: {self.last_value}"
