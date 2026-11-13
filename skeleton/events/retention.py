"""Physical deletion of expired outbox rows: the only delete path of DomainEvent."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

DEFAULT_RETENTION_DAYS = 30


def retention_days() -> int:
    return int(getattr(settings, "SKELETON_EVENT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))


def purge_expired_events() -> int:
    """Job handler (per organization, system context): delete events older than the retention."""
    from .models import DomainEvent

    cutoff = timezone.now() - timedelta(days=retention_days())
    expired = DomainEvent.objects.filter(occurred_at__lt=cutoff)
    total = 0
    while True:  # In blocks of 1 000 (#308); the manager keeps the organization filter.
        ids = list(expired.order_by("pk").values_list("pk", flat=True)[:1000])
        if not ids:
            return total
        deleted, _ = DomainEvent.objects.filter(pk__in=ids).delete()
        total += deleted
