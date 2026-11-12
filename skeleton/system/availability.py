"""Platform availability of installed apps: the gate above each organization's activation.

Installed ≠ available ≠ active. The platform decides availability (all
organizations or only selected ones); each organization still decides
activation in ``AppInstallation``. Both default closed: without an
availability row the app is unavailable, and without an ``AppInstallation``
it is disabled (#22). An unavailable app is closed in an
organization even if that organization enabled it. These services write only
global rows: RLS and one tenant per request forbid rewriting N organizations.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from skeleton.accounts.platform import is_platform_admin
from skeleton.common.exceptions import ConcurrentUpdate, MissingTenantContext
from skeleton.tenancy.context import current_context

from .app_registry import dependent_labels, get_app, required_labels


def _organization(organization_id: UUID | None) -> UUID:
    organization_id = organization_id or current_context().organization_id
    if organization_id is None:
        raise MissingTenantContext("Availability is decided per organization.")
    return organization_id


def availability_map(app_labels: Iterable[str], *, organization_id: UUID | None = None) -> dict[str, bool]:
    """Availability of several apps for one organization in two queries."""
    from .models import AppAvailability, AppOrganizationGrant

    labels = list(dict.fromkeys(app_labels))
    organization_id = _organization(organization_id)
    rows = {row.app_label: row for row in AppAvailability.objects.filter(app_label__in=labels)}
    selected = [row.pk for row in rows.values() if row.mode == AppAvailability.Mode.SELECTED]
    granted = set(
        AppOrganizationGrant.objects.filter(
            availability_id__in=selected, granted_organization_id=organization_id
        ).values_list("availability_id", flat=True)
    )
    # Closed by default (#22): without a row the app is unavailable to every organization.
    return {
        label: label in rows and (rows[label].mode == AppAvailability.Mode.ALL or rows[label].pk in granted)
        for label in labels
    }


def app_available(app_label: str, *, organization_id: UUID | None = None) -> bool:
    return availability_map([app_label], organization_id=organization_id)[app_label]


def available_with_dependencies(app_label: str, *, organization_id: UUID | None = None) -> bool:
    """The app and its whole transitive dependency closure are available to the organization."""
    labels = [app_label, *required_labels(app_label)]
    return all(availability_map(labels, organization_id=organization_id).values())


def _require_platform(user) -> None:
    context = current_context()
    if not is_platform_admin(user) or not context.platform_admin or context.organization_id is not None:
        raise PermissionDenied("Platform administration required.")


def _changed() -> None:
    from .applications import forget_application_states

    forget_application_states(everywhere=True)


def _locked_availability(app_label: str, *, create: bool):
    from .models import AppAvailability

    get_app(app_label)  # Only installed apps have an availability gate.
    row = AppAvailability.objects.select_for_update().filter(app_label=app_label).first()
    if row is None and create:
        try:
            with transaction.atomic():
                # Granting one organization never opens the app to all of them.
                row = AppAvailability(
                    app_label=app_label, mode=AppAvailability.Mode.SELECTED, upgrade_state=AppAvailability.UPGRADE_DONE
                )
                row.save()
        except IntegrityError:
            raise ConcurrentUpdate("Application availability changed.") from None
    return row


@transaction.atomic
def set_app_availability(app_label: str, *, user, mode: str, revision: int):
    """CAS on the availability row; ``revision=0`` is the virtual state before the first write."""
    from .models import AppAvailability

    _require_platform(user)
    _changed()
    if mode not in AppAvailability.Mode.values:
        raise ValidationError({"mode": "Use all ou selected."})
    if type(revision) is not int:
        raise ConcurrentUpdate("Application availability changed.")
    row = _locked_availability(app_label, create=False)
    if row is None:
        if revision != 0:
            raise ConcurrentUpdate("Application availability changed.")
        try:
            with transaction.atomic():
                row = AppAvailability(app_label=app_label, mode=mode, upgrade_state=AppAvailability.UPGRADE_DONE)
                row.save()
        except IntegrityError:
            raise ConcurrentUpdate("Application availability changed.") from None
        return row
    if row.revision != revision:
        raise ConcurrentUpdate("Application availability changed.")
    if row.mode != mode:
        row.mode = mode
        row.save(update_fields={"mode"})
    return row


def _active_organization(organization_id: UUID):
    from skeleton.tenancy.models import Organization

    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        raise ValidationError({"organization": "Organização indisponível."})
    return organization


@transaction.atomic
def grant_app(app_label: str, *, user, organization_id: UUID):
    """Admit an organization; re-granting restores the same row (and its prior activation choice)."""
    from .models import AppOrganizationGrant

    _require_platform(user)
    _changed()
    organization = _active_organization(organization_id)
    row = _locked_availability(app_label, create=True)
    missing = [
        label for label in required_labels(app_label) if not app_available(label, organization_id=organization.pk)
    ]
    if missing:
        message = f"Dependências indisponíveis para a organização: {', '.join(missing)}."
        raise ValidationError({"app_label": ValidationError(message, code="dependency_unavailable")})
    grant = AppOrganizationGrant.all_objects.filter(availability=row, granted_organization=organization).first()
    if grant is None:
        grant = AppOrganizationGrant(availability=row, granted_organization=organization)
        grant.save()
    elif grant.deleted_at is not None:
        grant.restore()
    return grant


@transaction.atomic
def revoke_app(app_label: str, *, user, organization_id: UUID, expected_revision: int | None = None):
    """Close the gate only; the organization's ``AppInstallation`` and configuration are preserved.

    ``expected_revision`` is the grant revision a form displayed: a stale form, or a grant
    already revoked, raises ``ConcurrentUpdate`` instead of acting on what nobody saw.
    """
    from .models import AppAvailability, AppOrganizationGrant

    _require_platform(user)
    _changed()
    row = _locked_availability(app_label, create=False)
    grant = (
        AppOrganizationGrant.objects.filter(availability=row, granted_organization_id=organization_id).first()
        if row
        else None
    )
    if expected_revision is not None and (
        grant is None or type(expected_revision) is not int or grant.revision != expected_revision
    ):
        raise ConcurrentUpdate("Application grant changed.")
    if grant is None:
        return None
    blocking = [
        label
        for label in dependent_labels(app_label)
        if AppOrganizationGrant.objects.filter(
            availability__app_label=label,
            availability__mode=AppAvailability.Mode.SELECTED,
            granted_organization_id=organization_id,
        ).exists()
    ]
    if blocking:
        message = f"Revogue antes as apps dependentes: {', '.join(blocking)}."
        raise ValidationError({"app_label": ValidationError(message, code="dependent_granted")})
    grant.delete()
    return grant


# --------------------------------------------------------------------------- platform read models (#23)


@dataclass(frozen=True)
class GrantSummary:
    organization_id: UUID
    name: str
    slug: str
    is_active: bool
    revision: int
    # Apps granted to this organization that require this one: revoking is refused until they go.
    blocked_by: tuple[str, ...]


@dataclass(frozen=True)
class AvailabilitySummary:
    """What /platform/ shows of one app's gate: global rows only, never tenant activation."""

    app_label: str
    mode: str | None  # None: no row yet, unavailable to every organization (#22).
    revision: int  # 0 is the virtual revision before the first write.
    grants: tuple[GrantSummary, ...]
    requires: tuple[str, ...]  # Must be available to an organization before it is granted this app.
    required_by: tuple[str, ...]  # Granted dependents block revoking this app.

    @property
    def granted_ids(self) -> frozenset[UUID]:
        return frozenset(grant.organization_id for grant in self.grants)


def availability_summary(app_label: str, *, user) -> AvailabilitySummary:
    """Mode, grants and dependency constraints of one installed app, in a fixed number of queries."""
    from .models import AppAvailability, AppOrganizationGrant

    _require_platform(user)
    get_app(app_label)
    row = AppAvailability.objects.filter(app_label=app_label).first()
    dependents = dependent_labels(app_label)
    grants: tuple[GrantSummary, ...] = ()
    if row is not None:
        blocking: dict[UUID, list[str]] = {}
        for organization_id, label in AppOrganizationGrant.objects.filter(
            availability__app_label__in=dependents,
            availability__mode=AppAvailability.Mode.SELECTED,
        ).values_list("granted_organization_id", "availability__app_label"):
            blocking.setdefault(organization_id, []).append(label)
        grants = tuple(
            GrantSummary(
                organization_id=grant.granted_organization_id,
                name=grant.granted_organization.name,
                slug=grant.granted_organization.slug,
                is_active=grant.granted_organization.is_active,
                revision=grant.revision,
                blocked_by=tuple(sorted(blocking.get(grant.granted_organization_id, ()))),
            )
            for grant in AppOrganizationGrant.objects.filter(availability=row)
            .select_related("granted_organization")
            .order_by("granted_organization__name", "granted_organization__slug")
        )
    return AvailabilitySummary(
        app_label=app_label,
        mode=row.mode if row else None,
        revision=row.revision if row else 0,
        grants=grants,
        requires=tuple(required_labels(app_label)),
        required_by=tuple(dependents),
    )


def availability_modes(app_labels: Iterable[str]) -> dict[str, str | None]:
    """The mode of each app (None without a row) in one query, for the installed apps list."""
    from .models import AppAvailability

    labels = list(dict.fromkeys(app_labels))
    rows = dict(AppAvailability.objects.filter(app_label__in=labels).values_list("app_label", "mode"))
    return {label: rows.get(label) for label in labels}
