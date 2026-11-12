"""The single definition of a platform administrator (docs/access-control.md).

A platform administrator is an authenticated, active, non-deleted identity with both
``is_staff`` and ``is_superuser``. Partial flags grant nothing. Every guard, context GUC,
audit scope, permission backend and template uses this predicate instead of reading flags.
"""

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext

PLATFORM_FLAGS = ("is_staff", "is_superuser")


class LastPlatformAdministrator(ValidationError):
    """The change would leave the platform without an active administrator (HTTP 409)."""

    def __init__(self) -> None:
        super().__init__(
            gettext("A plataforma precisa manter ao menos um administrador ativo."), code="last_platform_administrator"
        )


def is_platform_admin(user: Any) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False) is True
        and getattr(user, "is_active", False) is True
        and getattr(user, "deleted_at", None) is None
        and getattr(user, "is_staff", False) is True
        and getattr(user, "is_superuser", False) is True
    )


def has_partial_platform_flags(user: Any) -> bool:
    """Exactly one of the flags: shown as "Parcial", never treated as administrator."""
    return bool(getattr(user, "is_staff", False)) != bool(getattr(user, "is_superuser", False))


def require_platform_admin(user: Any) -> None:
    if not is_platform_admin(user):
        raise PermissionDenied("Platform administration required.")


def platform_admin_context(request: Any) -> dict[str, bool]:
    """Template context processor: ``platform_admin`` for the requesting identity."""
    return {"platform_admin": is_platform_admin(getattr(request, "user", None))}


def lock_platform_admins(*, using: str = "default") -> list[Any]:
    """Lock every active platform administrator in sysid order (deadlock-free serialization).

    Call it before locking the target identity, so two concurrent demotions queue on the
    same rows in the same order; the loser re-reads the set and sees the winner's change.
    """
    from skeleton.accounts.models import User

    if not transaction.get_connection(using).in_atomic_block:
        raise RuntimeError("lock_platform_admins() requires a transaction.")
    return list(
        User.all_objects.using(using)
        .select_for_update()
        .filter(is_active=True, deleted_at__isnull=True, is_staff=True, is_superuser=True)
        .order_by("sysid")
    )


def assert_platform_admin_remains(admins: list[Any], target: Any, *, keeps_role: bool) -> None:
    """Raise when ``target`` is in the locked admin set, loses the role and is the last one."""
    if keeps_role or all(admin.pk != target.pk for admin in admins):
        return
    if not any(admin.pk != target.pk for admin in admins):
        raise LastPlatformAdministrator()
