"""Periodic directory sync of an LDAP provider (#33).

The directory is read first (paged, outside any transaction). An empty result or a search
error aborts without writing. Then, in one transaction per organization under
``system_context(reason="directory:sync")``, memberships of people who logged in through
this provider and are no longer in the directory (or the required group) are deactivated
with the same lifecycle as an administrator would use, each in its own savepoint: the last
administrator is skipped and reported. Nobody is ever reactivated. ``provision`` (off by
default) admits people who logged in through the provider before but never had a
membership here, following ``IdentityProviderAdmission``. ``dry_run`` does all of it and
rolls back. The report carries counts and UUIDs only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

REASON = "directory:sync"


class _Rollback(Exception):
    pass


@dataclass
class SyncReport:
    provider_id: str
    organization_id: str
    directory_users: int = 0
    deactivated: list[str] = field(default_factory=list)
    skipped_last_admin: list[str] = field(default_factory=list)
    provisioned: list[str] = field(default_factory=list)
    requested: list[str] = field(default_factory=list)
    dry_run: bool = False
    aborted: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _record(row: Any, code: str) -> None:
    from skeleton.accounts.models import IdentityProvider

    with transaction.atomic():
        locked = IdentityProvider.objects.select_for_update().get(pk=row.pk)
        locked.last_sync_at = timezone.now()
        locked.last_sync_code = code[:40]
        locked.save(update_fields={"last_sync_at", "last_sync_code"})


def sync_provider(
    row: Any, *, dry_run: bool = False, provision: bool = False, max_deactivations: int | None = None
) -> SyncReport:
    from allauth.socialaccount.models import SocialAccount

    from skeleton.access.services import LastAdministrator, admit_member, system_deactivate_membership
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import IdentityProviderAdmission, JoinRequest, Membership, Organization

    from . import client

    report = SyncReport(provider_id=row.provider_id, organization_id=str(row.bound_organization_id), dry_run=dry_run)
    try:
        options = client.options_from(row.directory, row.settings or {})
        present = {person.uid for person in client.iterate_users(options, row.secret.reveal() if row.secret else "")}
    except client.DirectoryError as exc:
        report.aborted = exc.code
    except ValueError:
        report.aborted = "invalid_settings"
    else:
        report.directory_users = len(present)
        if not present:
            report.aborted = "empty_directory"  # Never read an empty answer as "everyone left".
    if report.aborted:
        if not dry_run:
            _record(row, report.aborted)
        return report
    # Materialized before any tenant transaction: global rows only.
    accounts = list(SocialAccount.objects.filter(provider=row.provider_id).values_list("uid", "user_id"))
    gone = sorted({user_id for uid, user_id in accounts if uid not in present}, key=str)
    staying = sorted({user_id for uid, user_id in accounts if uid in present}, key=str)
    try:
        with system_context(reason=REASON, organization_id=row.bound_organization_id), transaction.atomic():
            organization = Organization.objects.select_for_update().filter(pk=row.bound_organization_id).first()
            if organization is None or not organization.is_active:
                report.aborted = "organization_inactive"
                raise _Rollback
            targets = list(Membership.objects.filter(user_id__in=gone, is_active=True).order_by("user_id"))
            if max_deactivations is not None and len(targets) > max_deactivations:
                report.aborted = "too_many_deactivations"
                raise _Rollback
            for membership in targets:
                try:
                    with transaction.atomic():
                        system_deactivate_membership(membership)
                except LastAdministrator:
                    report.skipped_last_admin.append(str(membership.user_id))
                else:
                    report.deactivated.append(str(membership.user_id))
            if provision:
                _provision(organization, row, staying, report, admit_member, IdentityProviderAdmission, JoinRequest)
            if dry_run:
                raise _Rollback
    except _Rollback:
        pass
    if not dry_run:
        _record(row, report.aborted or "ok")
    return report


def _provision(organization, row, staying, report, admit_member, admission_model, join_request_model) -> None:
    from django.contrib.auth import get_user_model

    from skeleton.access.models import Role
    from skeleton.tenancy.admission import REQUEST_TTL
    from skeleton.tenancy.models import Membership

    rules = admission_model.objects.filter(provider=row).first()
    if rules is None or not rules.jit_provisioning:
        return
    role = (
        Role.objects.filter(pk=rules.default_role_id).first()
        if rules.default_role_id
        else Role.objects.get(code="member", is_builtin=True)
    )
    known = set(Membership.all_objects.filter(user_id__in=staying).values_list("user_id", flat=True))
    users = get_user_model().objects.filter(pk__in=[pk for pk in staying if pk not in known], is_active=True)
    for user in users.order_by("pk"):
        # Never anyone who had a membership here: a former member is a human decision.
        if rules.jit_requires_approval:
            if not join_request_model.objects.filter(user=user, status__in=join_request_model.OPEN).exists():
                join_request_model(
                    organization=organization,
                    user=user,
                    channel=join_request_model.Channel.SSO,
                    role=role,
                    status=join_request_model.Status.PENDING,
                    expires_at=timezone.now() + REQUEST_TTL,
                ).save()
                report.requested.append(str(user.pk))
        else:
            admit_member(organization=organization, user=user, role=role)
            report.provisioned.append(str(user.pk))


def providers(provider_id: str | None) -> list[Any]:
    from skeleton.accounts.models import IdentityProvider

    rows = IdentityProvider.objects.filter(protocol="ldap", enabled=True).select_related("bound_organization")
    if provider_id:
        rows = rows.filter(provider_id=provider_id)
    return list(rows.order_by("provider_id"))


def provider_ids(rows: list[Any]) -> list[UUID]:
    return [row.pk for row in rows]
