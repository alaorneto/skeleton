"""What the platform may read and change in an organization: administrator overview and designation."""

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate


@dataclass(frozen=True)
class AdministratorSummary:
    user_id: UUID
    email: str
    direct: bool


@dataclass(frozen=True)
class ApplicationSummary:
    """One installed app as the platform sees it in an organization: gates only, no configuration."""

    app_label: str
    label: str
    available: bool  # Platform gate (global rows).
    enabled: bool  # The organization's own choice in AppInstallation.
    effective: bool  # Both gates open for the app and its whole dependency closure.


@dataclass(frozen=True)
class OrganizationOverview:
    """What the platform may see of an organization: counts, administrator emails and app gates."""

    organization_id: UUID
    active_members: int
    administrators: tuple[AdministratorSummary, ...]
    administrators_version: str
    applications: tuple[ApplicationSummary, ...] = ()


@contextmanager
def _read_only(using):
    """Refuse any statement but plain reads while the platform inspects a tenant."""
    from django.db import connections

    allowed = ("SELECT", "SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")

    def guard(execute, sql, params, many, context):
        statement = sql.lstrip().upper()
        if not statement.startswith(allowed) or " FOR UPDATE" in statement or " FOR SHARE" in statement:
            raise PermissionDenied("Platform inspection is read-only.")
        return execute(sql, params, many, context)

    with connections[using].execute_wrapper(guard):
        yield


def _administrators(using):
    """Administrators of the current tenant and a version token of that set (the form's CAS)."""
    from skeleton.access.models import MemberRole
    from skeleton.access.services import administrator_memberships

    direct = set(
        MemberRole.objects.using(using)
        .filter(role__code="organization_admin", role__is_builtin=True, role__deleted_at__isnull=True)
        .values_list("membership_id", flat=True)
    )
    rows = administrator_memberships().using(using).values_list("pk", "user_id", "user__email").order_by("user__email")
    summaries = tuple(
        AdministratorSummary(user_id=user_id, email=email, direct=pk in direct) for pk, user_id, email in rows
    )
    token = "\n".join(sorted(f"{pk}:{pk in direct}" for pk, _, _ in rows))
    return summaries, hashlib.sha256(token.encode()).hexdigest()


def organization_overview(*, actor, organization, using="default"):
    """Read-only platform view of one organization; never members' identities or business rows."""
    from skeleton.accounts.platform import require_context_actor, require_platform_admin
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Membership

    require_platform_admin(actor)
    require_context_actor(actor)
    with (
        system_context(reason="platform:organization-inspect", organization_id=organization.pk, using=using),
        _read_only(using),
    ):
        administrators, version = _administrators(using)
        return OrganizationOverview(
            organization_id=organization.pk,
            active_members=Membership.objects.using(using).filter(is_active=True).count(),
            administrators=administrators,
            administrators_version=version,
            applications=_applications(organization.pk, using),
        )


def _applications(organization_id, using):
    """Availability and activation of every installed app in the current (inspected) tenant."""
    from skeleton.system.app_registry import all_apps, required_labels
    from skeleton.system.availability import availability_map
    from skeleton.system.models import AppInstallation

    manifests = sorted(all_apps(), key=lambda manifest: manifest.label)
    labels = [manifest.app_label for manifest in manifests]
    if not labels:
        return ()
    available = availability_map(labels, organization_id=organization_id)
    enabled = dict(
        AppInstallation.objects.using(using).filter(app_label__in=labels).values_list("app_label", "enabled")
    )
    open_gate = {label: available[label] and enabled.get(label, False) for label in labels}
    return tuple(
        ApplicationSummary(
            app_label=manifest.app_label,
            label=manifest.label,
            available=available[manifest.app_label],
            enabled=enabled.get(manifest.app_label, False),
            effective=open_gate[manifest.app_label]
            and all(open_gate.get(label, False) for label in required_labels(manifest.app_label)),
        )
        for manifest in manifests
    )


def designate_organization_admin(*, actor, organization, user, replace=None, expected_version=None, using="default"):
    """A platform administrator designates (and optionally replaces) an organization administrator.

    Runs under ``system_context(reason="organization:designate-admin")`` for that organization
    only, so the audit events carry the platform actor and that source. ``expected_version`` is
    the ``administrators_version`` the form displayed; a different set raises ``ConcurrentUpdate``.
    ``replace`` loses only its direct ``organization_admin`` role; the organization always keeps an
    active administrator (``LastAdministrator`` otherwise). The platform cannot designate itself:
    that would turn platform administration into organization access.
    """
    from skeleton.access.models import MemberRole, Role
    from skeleton.access.services import ensure_last_admin, organization_lock, provision_membership
    from skeleton.accounts.platform import require_context_actor, require_platform_admin
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Membership

    require_platform_admin(actor)
    require_context_actor(actor)
    if user.pk == actor.pk:
        raise PermissionDenied(gettext("A administração da plataforma não se designa numa organização."))
    if replace is not None and replace.pk == user.pk:
        raise ValidationError({"replace": gettext("A pessoa substituída precisa ser outra.")}, code="invalid")
    with (
        transaction.atomic(using=using),
        system_context(reason="organization:designate-admin", organization_id=organization.pk, using=using),
        organization_lock() as locked,
    ):
        if expected_version is not None and _administrators(using)[1] != expected_version:
            raise ConcurrentUpdate("The administrators changed since the form was displayed.")
        role = Role.objects.using(using).get(code="organization_admin", is_builtin=True)
        membership = provision_membership(organization=locked, user=user, role=role)
        if replace is not None:
            replaced = Membership.objects.using(using).filter(user=replace).first()
            link = MemberRole.objects.using(using).filter(membership=replaced, role=role).first() if replaced else None
            if link is None:
                raise ValidationError(
                    {"replace": gettext("A pessoa substituída não é administradora direta.")}, code="invalid"
                )
            link.delete(using=using)
        ensure_last_admin()
        return membership
