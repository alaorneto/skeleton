from django.core.validators import MaxLengthValidator
from django.db import models

from skeleton.common.models import OrganizationRecord

MAX_BODY = 5000


class Comment(OrganizationRecord):
    """A plain-text comment on a record; authorization always derives from the target.

    The author is ``created_by`` (the actor of the creating context). Editing sets
    ``edited_at``; deletion is the soft delete of every record and restore keeps the UUID.
    """

    target_table = models.ForeignKey("sk_system.TableDefinition", on_delete=models.PROTECT, related_name="comments")
    target_sysid = models.UUIDField()
    body = models.TextField(validators=[MaxLengthValidator(MAX_BODY)])
    edited_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("target_table_id", "target_sysid")

    class Meta(OrganizationRecord.Meta):
        indexes = [
            models.Index(
                fields=("organization", "target_table", "target_sysid", "created_at"), name="sk_comments_target"
            )
        ]

    def __str__(self):
        return f"Comment {self.pk}"
