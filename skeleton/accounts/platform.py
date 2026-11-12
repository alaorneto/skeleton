"""The single definition of a platform administrator (docs/access-control.md).

A platform administrator is an authenticated, active, non-deleted identity with both
``is_staff`` and ``is_superuser``. Partial flags grant nothing. Every guard, context GUC,
audit scope, permission backend and template uses this predicate instead of reading flags.
"""

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

PLATFORM_FLAGS = ("is_staff", "is_superuser")


class LastPlatformAdministrator(ValidationError):
    """The change would leave the platform without an active administrator (HTTP 409)."""

    def __init__(self) -> None:
        super().__init__(
            "A plataforma precisa manter ao menos um administrador ativo.", code="last_platform_administrator"
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


def platform_admin_context(request: Any) -> dict[str, Any]:
    """Template context processor: ``platform_admin`` and the lazy ``platform_capabilities`` (#34)."""
    from django.utils.functional import SimpleLazyObject

    user = getattr(request, "user", None)
    return {
        "platform_admin": is_platform_admin(user),
        "platform_capabilities": SimpleLazyObject(lambda: platform_capabilities(user)),
    }


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


# --------------------------------------------------------------------------- limited platform roles (#34)

AUDIT_READ = "audit.read"
INVENTORY_READ = "inventory.read"
UPDATES_OPERATE = "updates.operate"
PLATFORM_ROLES = {
    "auditor": frozenset({AUDIT_READ, INVENTORY_READ}),
    "operator": frozenset({UPDATES_OPERATE}),
}
PLATFORM_CAPABILITIES = frozenset().union(*PLATFORM_ROLES.values())


def platform_capabilities(user: Any) -> frozenset[str]:
    """What ``user`` may do on the platform, recomputed on every call (revocation is immediate)."""
    if is_platform_admin(user):
        return PLATFORM_CAPABILITIES
    if not (
        user is not None
        and getattr(user, "is_authenticated", False) is True
        and getattr(user, "is_active", False) is True
        and getattr(user, "deleted_at", None) is None
        and not getattr(user, "is_service_account", False)
    ):
        return frozenset()
    from skeleton.accounts.models import PlatformRoleAssignment

    roles = PlatformRoleAssignment.objects.filter(user_id=user.pk).values_list("role", flat=True)
    result: set[str] = set()
    for role in roles:
        result |= PLATFORM_ROLES.get(role, frozenset())
    return frozenset(result)


def has_platform_capability(user: Any, capability: str) -> bool:
    if capability not in PLATFORM_CAPABILITIES:
        raise ValueError(f"Unknown platform capability: {capability}")
    return capability in platform_capabilities(user)


def require_platform_capability(user: Any, capability: str) -> None:
    if not has_platform_capability(user, capability):
        raise PermissionDenied("Platform capability required.")


def _bump(user_id: Any) -> None:
    from skeleton.accounts.models import User

    locked = User.all_objects.select_for_update().get(pk=user_id)
    locked.auth_version += 1
    locked.save(update_fields={"auth_version"})


def grant_platform_role(*, actor: Any, user: Any, role: str) -> Any:
    """A platform administrator grants a limited role; the identity's sessions are renewed."""
    from skeleton.accounts.models import PlatformRoleAssignment

    require_platform_admin(actor)
    if role not in PLATFORM_ROLES:
        raise ValidationError({"role": "Use auditor ou operator."})
    if user.pk == actor.pk or not user.is_active or user.deleted_at is not None or user.is_service_account:
        raise ValidationError({"user": "Escolha outra identidade ativa."})
    with transaction.atomic():
        existing = PlatformRoleAssignment.objects.filter(user=user, role=role).first()
        if existing is not None:
            return existing
        record = PlatformRoleAssignment(user=user, role=role)
        record.save()
        _bump(user.pk)
        return record


def revoke_platform_role(*, actor: Any, assignment_id: Any, expected_revision: Any) -> Any:
    """Revoke with CAS; takes effect on the identity's next request."""
    from skeleton.accounts.models import PlatformRoleAssignment
    from skeleton.common.exceptions import ConcurrentUpdate

    require_platform_admin(actor)
    with transaction.atomic():
        record = PlatformRoleAssignment.objects.select_for_update().filter(pk=assignment_id).first()
        if record is None:
            raise ValidationError({"role": "Papel inexistente."})
        if type(expected_revision) is not int or record.revision != expected_revision:
            raise ConcurrentUpdate("Platform role changed.")
        record.delete()
        _bump(record.user_id)
        return record
