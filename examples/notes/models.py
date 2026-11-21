from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import Q

from skeleton.common.models import SecuredRecord, SystemRecord


class Note(SecuredRecord):
    code = models.CharField(max_length=64)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, validators=[MaxLengthValidator(10000)])

    class Meta(SecuredRecord.Meta):
        constraints = SecuredRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "code"),
                condition=Q(deleted_at__isnull=True),
                name="example_notes_note_live_code",
                violation_error_code="unique",
            ),
        ]
        ordering = ("created_at", "sysid")

    def __str__(self):
        return self.title


class NoteCategory(SystemRecord):
    """Global reference catalog shared by every organization (#576), loaded by an operator command.

    A global table of an app: written only in a system context without organization, that is by
    ``manage.py load_note_categories`` (``command_context``); never by a view, service or consumer.
    """

    code = models.CharField(max_length=64)
    title = models.CharField(max_length=200)

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("code",),
                condition=Q(deleted_at__isnull=True),
                name="example_notes_category_live_code",
                violation_error_code="unique",
            ),
        ]
        ordering = ("code",)

    def __str__(self):
        return self.title
