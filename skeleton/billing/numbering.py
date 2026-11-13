"""Gapless invoice numbers, platform wide and per UTC year (#394).

The platform issues every invoice, so the numbering is global, not the per-organization
``skeleton.sequences``. The year's counter row is locked ``FOR UPDATE`` in the caller's
transaction: a rollback of the invoice gives the number back and the next one reuses it.
The first creation of a year's row races on the live-year unique constraint inside a
savepoint; the loser rereads the winner's row under lock. No conflict-skipping upsert:
the project forbids them (AGENTS.md). Invoices are therefore numbered
one at a time across the platform, which is the price of having no gaps.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext


def _prefix() -> str:
    return str(getattr(settings, "SKELETON_BILLING_INVOICE_PREFIX", "") or "")


def format_number(year: int, number: int) -> str:
    return f"{_prefix()}{year:04d}-{number:06d}"


def next_invoice_number(*, at: datetime | None = None) -> str:
    """The next invoice number of the UTC year of ``at`` (default now), as ``<prefix>YYYY-NNNNNN``."""
    from .models import InvoiceCounter

    moment = at or timezone.now()
    if timezone.is_naive(moment):
        raise ValidationError({"at": gettext("Use data e hora com fuso.")})
    year = moment.astimezone(UTC).year
    with transaction.atomic():
        counter = InvoiceCounter.objects.select_for_update().filter(year=year).first()
        if counter is None:
            try:
                with transaction.atomic():
                    counter = InvoiceCounter(year=year, last_value=0)
                    counter.save()
            except (IntegrityError, ValidationError):
                # Another transaction created the year's row first (committed: the model's
                # unique validation; still in flight: the database once it commits).
                counter = None
            if counter is None:
                counter = InvoiceCounter.objects.select_for_update().get(year=year)
            # Otherwise the row is ours and still uncommitted: nobody else can read it.
        counter.last_value += 1
        counter.save(update_fields={"last_value"})
    return format_number(year, counter.last_value)
