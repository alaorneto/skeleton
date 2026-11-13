"""Organization lifecycle: creation with builtin roles and the first administrator, and platform-side edits."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate


def update_organization(organization, *, actor, data, expected_revision, using="default"):
    """Platform-side organization edit with CAS; the slug is immutable."""
    from skeleton.accounts.platform import require_platform_actor
    from skeleton.tenancy.models import Organization

    require_platform_actor(actor)
    unknown = set(data) - {"name", "is_active"}
    if unknown:
        raise ValidationError(gettext("Campo não permitido."))
    with transaction.atomic(using=using):
        previous = Organization.objects.using(using).select_for_update(no_key=True).get(pk=organization.pk)
        if expected_revision != previous.revision:
            raise ConcurrentUpdate("The organization form is stale.")
        changed = {key for key, value in data.items() if getattr(previous, key) != value}
        if not changed:
            return previous
        for key in changed:
            setattr(previous, key, data[key])
        previous.save(using=using, update_fields=changed)
        return previous


def create_organization(*, owner, slug, name, using="default"):
    """Runs under system_context(reason="organization:create"), which HTTP opens only for the platform."""
    from skeleton.access.capabilities import BUILTIN_ROLES
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership, reconcile_builtin_roles
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Organization

    with transaction.atomic(using=using):
        with system_context(reason="organization:create", using=using):
            if not owner.is_active or owner.deleted_at:
                raise ValidationError(gettext("O responsável pela organização precisa estar ativo."))
            organization = Organization(name=name, slug=slug)
            organization.save(using=using)
        with system_context(reason="organization:bootstrap", organization_id=organization.pk, using=using):
            roles = {}
            for code, builtin in BUILTIN_ROLES.items():
                roles[code] = Role(organization=organization, code=code, name=builtin.name, is_builtin=True)
                roles[code].save(using=using)
            provision_membership(organization=organization, user=owner, role=roles["organization_admin"])
        reconcile_builtin_roles(organization_id=organization.pk, using=using)
        from skeleton.common.organization_hooks import organization_created

        with system_context(reason="organization:bootstrap", organization_id=organization.pk, using=using):
            # Subsystems react in the same transaction (billing's default plan, #396).
            organization_created(organization)
    return organization
