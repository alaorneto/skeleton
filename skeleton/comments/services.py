"""Comments inherit authorization from their target record, like attachments.

- Reading a record's comments needs read on the record; so does commenting, on a table whose
  ``TableSpec.comments`` opts in. A target of another organization, unreadable, deleted or
  on a table without the policy answers 404.
- Only the author edits. The author and the organization administrator delete (moderation)
  and restore. Deleted comments appear only to them, to be restored.
- Changes require the revision the person saw (CAS); the database audits every write.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate

from .text import clean_body


def policy(spec: Any) -> Any:
    from skeleton.common.models import SecuredRecord

    if getattr(spec, "comments", None) is None or not spec.expose_api or not issubclass(spec.model, SecuredRecord):
        raise Http404  # Tables without opt-in have no comment surface at all.
    return spec.comments


def readable_target(spec: Any, target_sysid: Any, *, user: Any) -> Any:
    from skeleton.access.policies import authorized_queryset

    policy(spec)
    try:
        record = authorized_queryset(spec.model, user=user, action="read").filter(pk=target_sysid).first()
    except PermissionDenied:
        record = None
    if record is None:
        raise Http404
    return record


def _table(spec: Any) -> Any:
    from skeleton.system.models import TableDefinition

    return TableDefinition.objects.get(model_label=spec.model._meta.label_lower, available=True)


def is_moderator(user: Any) -> bool:
    from skeleton.access.policies import is_organization_admin

    return is_organization_admin(user)


def comments_for(spec: Any, target_sysid: Any, *, user: Any, include_deleted: bool = False) -> Any:
    """Live comments of a readable target, oldest first; with ``include_deleted``, also the
    deleted ones the person may restore (their own, or every one for the administrator)."""
    from django.db.models import Q

    from .models import Comment

    record = readable_target(spec, target_sysid, user=user)
    queryset = Comment.objects.with_deleted() if include_deleted else Comment.objects.all()
    queryset = queryset.filter(target_table=_table(spec), target_sysid=record.pk)
    if include_deleted and not is_moderator(user):
        queryset = queryset.filter(Q(deleted_at__isnull=True) | Q(created_by=user.pk))
    return queryset.order_by("created_at", "sysid")


def add(spec: Any, target_sysid: Any, *, user: Any, body: Any) -> Any:
    from .models import Comment

    record = readable_target(spec, target_sysid, user=user)
    comment = Comment(
        organization_id=record.organization_id,
        target_table=_table(spec),
        target_sysid=record.pk,
        body=clean_body(body),
    )
    comment.full_clean(exclude=("organization", "target_table"))
    comment.save()
    return comment


def _locked(spec: Any, target_sysid: Any, comment_id: Any, *, user: Any, deleted: bool = False) -> Any:
    from .models import Comment

    record = readable_target(spec, target_sysid, user=user)
    queryset = Comment.objects.with_deleted() if deleted else Comment.objects.all()
    comment = (
        queryset.select_for_update()
        .filter(pk=comment_id, target_table=_table(spec), target_sysid=record.pk)
        .filter(deleted_at__isnull=not deleted)
        .first()
    )
    if comment is None:
        raise Http404
    return comment


def _check_revision(comment: Any, revision: int) -> None:
    if type(revision) is not int or comment.revision != revision:
        raise ConcurrentUpdate("Comment revision changed.")


def edit(spec: Any, target_sysid: Any, comment_id: Any, *, user: Any, body: Any, revision: int) -> Any:
    comment = _locked(spec, target_sysid, comment_id, user=user)
    if comment.created_by != user.pk:
        raise PermissionDenied("Only the author edits a comment.")
    _check_revision(comment, revision)
    comment.body = clean_body(body)
    comment.edited_at = timezone.now()
    comment.save(update_fields={"body", "edited_at"})
    return comment


def _moderated(comment: Any, user: Any) -> None:
    if comment.created_by != user.pk and not is_moderator(user):
        raise PermissionDenied("Only the author or the organization administrator.")


def remove(spec: Any, target_sysid: Any, comment_id: Any, *, user: Any, revision: int) -> Any:
    comment = _locked(spec, target_sysid, comment_id, user=user)
    _moderated(comment, user)
    _check_revision(comment, revision)
    comment.delete()
    return comment


def restore(spec: Any, target_sysid: Any, comment_id: Any, *, user: Any, revision: int) -> Any:
    comment = _locked(spec, target_sysid, comment_id, user=user, deleted=True)
    _moderated(comment, user)
    _check_revision(comment, revision)
    comment.restore()
    return comment


COMMENT_FIELDS = ("sysid", "revision", "created_at", "created_by", "edited_at", "deleted_at", "body")


def comment_data(comment: Any) -> dict[str, Any]:
    data = {name: getattr(comment, name) for name in COMMENT_FIELDS}
    data["author_id"] = data.pop("created_by")
    return data


def comment_schema() -> dict[str, Any]:
    """OpenAPI schema of :func:`comment_data`, typed by ``_meta``."""
    from skeleton.api.openapi import UUID, model_fields, nullable

    names = tuple(name for name in COMMENT_FIELDS if name != "created_by")
    return model_fields(_model(), names, author_id=nullable(UUID))


def _model() -> Any:
    from .models import Comment

    return Comment
