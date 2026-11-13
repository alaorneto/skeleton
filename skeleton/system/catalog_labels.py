"""Label and description of the table catalog, edited by the platform with CAS (#265).

Metadata only: changing a label never runs DDL.
"""

from django.db import transaction

from skeleton.common.exceptions import ConcurrentUpdate


def update_catalog_labels(model, pk, data, *, expected_revision):
    """Lock the row, compare the revision, and save only the fields that changed."""
    with transaction.atomic():
        record = model.objects.select_for_update().get(pk=pk)
        if expected_revision != record.revision:
            raise ConcurrentUpdate("The catalog form is stale.")
        changed = {name for name in ("label", "description") if getattr(record, name) != data[name]}
        if changed:
            for name in changed:
                setattr(record, name, data[name])
            record.save(update_fields=changed)
        return record
