"""Contract of a sealable model of an app (#530): what the seal freezes, proved on the runtime role.

``assert_sealed_contract`` seals a record made by the app and checks, collecting every problem:
a frozen column changes neither by the model nor by SQL; a free column still changes; deletion is
refused unless the model allows it; a child (``make_child``) is refused; the attachments are
frozen when the policy says so; ``verify_seal`` holds; another organization neither sees nor seals
the record. Tests only.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

__all__ = ["assert_sealed_contract"]


def _changed(field: Any, value: Any) -> Any:
    """A different value of the same type for a text or integer column; ``None`` when unknown."""
    kind = field.get_internal_type()
    if kind in ("CharField", "TextField", "SlugField", "EmailField"):
        text = (value or "") + "x"
        return text[: field.max_length] if field.max_length else text
    if kind in (
        "IntegerField",
        "PositiveIntegerField",
        "BigIntegerField",
        "PositiveBigIntegerField",
        "SmallIntegerField",
    ):
        return (value or 0) + 1
    return None


def _sql_refused(model: Any, assignment: str, pk: Any, message: str) -> bool:
    from django.db import DatabaseError, connection, transaction

    table = connection.ops.quote_name(model._meta.db_table)
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"UPDATE skeleton.{table} SET {assignment}, revision = revision + 1 WHERE sysid = %s", [pk])  # noqa: S608 - test kit, quoted identifiers
    except DatabaseError as exc:
        return message in str(exc)
    return False


def _quoted(name: str) -> str:
    from django.db import connection

    return str(connection.ops.quote_name(name))


def _updated_rows(model: Any, pk: Any) -> int:
    """Rows a write of another organization reaches (RLS answers zero)."""
    from django.db import connection, transaction

    with transaction.atomic(), connection.cursor() as cursor:
        table = _quoted(model._meta.db_table)
        statement = f"UPDATE skeleton.{table} SET updated_at = updated_at WHERE sysid = %s"  # noqa: S608 - quoted identifiers
        cursor.execute(statement, [pk])
        return int(cursor.rowcount)


def _free_field(model: Any) -> Any:
    from skeleton.common.field_names import ACCESS_FIELD_NAMES, SEAL_FIELD_NAMES, SYSTEM_FIELD_NAMES

    frozen = set(model.seal_attnames())
    reserved = {*SYSTEM_FIELD_NAMES, *ACCESS_FIELD_NAMES, *SEAL_FIELD_NAMES, "organization", "organization_id"}
    for field in model._meta.concrete_fields:
        if field.attname in frozen or {field.name, field.attname} & reserved or field.is_relation or field.primary_key:
            continue
        if field.name in model.immutable_fields or field.attname in model.immutable_fields:
            continue
        if _changed(field, None) is not None:
            return field
    return None


def assert_sealed_contract(
    model: Any,
    *,
    make_record: Callable[[], Any],
    seal: Callable[[Any], Any],
    acting: Callable[[], AbstractContextManager[Any]],
    foreign: Callable[[], AbstractContextManager[Any]] | None = None,
    make_child: Callable[[Any], Any] | None = None,
    extra: Callable[[Any], Any] | None = None,
) -> None:
    """Fail with every problem of the seal contract of ``model`` at once.

    ``acting()`` opens the context of a member of organization A that may update the records;
    ``make_record()`` creates an unsealed record there and ``seal(record)`` seals it the app's
    way (for example through ``seal_record``) and returns it. ``foreign()`` opens a context of
    organization B (A/B); ``make_child(record)`` writes a child row (``seal_parent``) of the
    record and must be refused once it is sealed; ``extra(record)`` is the ``extra`` the app
    passes to the digest.
    """
    from django.db import transaction

    from skeleton.common.exceptions import RecordSealed
    from skeleton.common.models import SealableMixin
    from skeleton.records.sealing import seal_payload, verify_seal
    from skeleton.system.registry import get_spec

    problems: list[str] = []
    if not (isinstance(model, type) and issubclass(model, SealableMixin)):
        raise AssertionError(f"{model!r} is not sealable.")
    records: Any = model  # The concrete record class: its managers are those of SystemRecord.

    def refused(action: Callable[[], Any]) -> bool:
        try:
            with transaction.atomic():
                action()
        except RecordSealed:
            return True
        return False

    with acting():
        record = make_record()
        if record.sealed_at is not None:
            problems.append("make_record() must return an unsealed record.")
        sealed = seal(record)
        pk = sealed.pk
        if records.all_objects.filter(pk=pk, sealed_at__isnull=False).count() != 1:
            problems.append("seal(record) did not persist the seal.")
        for attname in model.seal_attnames():
            field = next(f for f in model._meta.concrete_fields if f.attname == attname)
            fresh = records.objects.get(pk=pk)
            value = _changed(field, getattr(fresh, attname))
            if value is not None:

                def change(fresh: Any = fresh, attname: str = attname, value: Any = value) -> None:
                    setattr(fresh, attname, value)
                    fresh.save()

                if not refused(change):
                    problems.append(f"Changing the frozen column {attname} through the model was not refused.")
            if not _sql_refused(model, f"{_quoted(str(field.column))} = NULL", pk, "sealed record"):
                problems.append(f"Changing the frozen column {field.column} by SQL was not refused by the guard.")
        if not _sql_refused(model, "sealed_by = NULL", pk, "sealed record"):
            problems.append("Changing the seal itself by SQL was not refused by the guard.")
        free = _free_field(model)
        if free is not None:
            fresh = records.objects.get(pk=pk)
            setattr(fresh, free.attname, _changed(free, getattr(fresh, free.attname)))
            try:
                with transaction.atomic():
                    fresh.save()
            except RecordSealed:
                problems.append(f"The free column {free.attname} could not change after the seal.")
        if make_child is not None and not refused(lambda: make_child(records.objects.get(pk=pk))):
            problems.append("A child of the sealed record was written.")
        try:
            policy = get_spec(model._meta.label_lower).seal
        except KeyError:
            policy = None
        current = records.objects.get(pk=pk)
        if policy is not None and policy.attachments == "frozen":
            from skeleton.records.sealing import require_open

            if not refused(lambda: require_open(get_spec(model._meta.label_lower), current, "attachments")):
                problems.append("The attachments of the sealed record are not frozen by the policy.")
            if "attachments" not in seal_payload(current):
                problems.append("The frozen attachments are not part of the seal digest.")
        if not verify_seal(current, extra=extra(current) if extra else None):
            problems.append("verify_seal() does not hold right after sealing.")
        # Last: a deletion that is not refused would hide the record from the checks above.
        if not model.seal_allows_delete:
            if not refused(lambda: records.objects.get(pk=pk).delete()):
                problems.append("Deleting a sealed record through the model was not refused.")
            if not _sql_refused(model, "deleted_at = now()", pk, "sealed record"):
                problems.append("Deleting a sealed record by SQL was not refused by the guard.")
    if foreign is not None:
        with foreign():
            if records.all_objects.filter(pk=pk).exists():
                problems.append("Another organization reads the sealed record.")
            if _updated_rows(model, pk):
                problems.append("Another organization reaches the sealed record by SQL.")
    if problems:
        raise AssertionError(f"{model._meta.label} violates the seal contract:\n- " + "\n- ".join(problems))
