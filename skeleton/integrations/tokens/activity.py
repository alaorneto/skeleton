"""Last use of API tokens (#48): operational, at most one write per token per window.

``touch(token)`` runs after a successful token authentication. It skips the database while the
cache says this token was recorded within ``ACTIVITY_WINDOW_S`` (5 minutes), and otherwise
updates ``ApiTokenActivity`` only when the stored ``last_used_at`` is older than the window.
The row is operational: no revision, no audit event, never exposed by the gateway.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

ACTIVITY_WINDOW_S = 300


def touch(token: Any, *, using: str = "default") -> bool:
    """Record a use of ``token``; returns whether a row was written."""
    from skeleton.tenancy.context import system_context

    from .models import ApiTokenActivity

    marker = f"skeleton:token-used:{token.pk}"
    try:
        if not cache.add(marker, 1, timeout=ACTIVITY_WINDOW_S):
            return False
    except Exception:  # noqa: BLE001, S110 - without the cache, the database check below still bounds writes
        pass
    now = timezone.now()
    with system_context(reason="api-token:activity", using=using):
        changed = (
            ApiTokenActivity.objects.using(using)
            .filter(token_id=token.pk, last_used_at__lt=now - timedelta(seconds=ACTIVITY_WINDOW_S))
            .update(last_used_at=now)
        )
        if changed:
            return True
        if ApiTokenActivity.objects.using(using).filter(token_id=token.pk).exists():
            return False
        try:
            with transaction.atomic(using=using):
                ApiTokenActivity(token_id=token.pk, last_used_at=now).save(using=using)
        except IntegrityError:
            return False  # A concurrent request recorded it first.
        return True


def last_used(token_ids) -> dict:
    from .models import ApiTokenActivity

    return dict(ApiTokenActivity.objects.filter(token_id__in=list(token_ids)).values_list("token_id", "last_used_at"))
