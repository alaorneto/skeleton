"""Bundles of apps: the tiers of a product (#714).

A bundle is a named set of installed apps. Assigning it to an organization activates the apps
it brings (offering them in ``selected`` mode) and deactivates the ones of the previous bundle
that it does not keep, in one transaction, and records ``OrganizationBundle``. The member limit
follows, because each app adds what the platform defined for it (#696). With ``lock_apps``, the
organization cannot turn the bundle's apps off (#701). With billing on, a paid app without the
right is refused (``app_not_entitled``): the platform contracts it in the organization's billing.

Only the platform administrator writes bundles and assigns them; the self-service signup (#715)
assigns the chosen bundle through ``apply_bundle`` in the signup's system context.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import system_context

from .app_registry import all_apps, required_labels

BUNDLE_REASON = "platform:apps"
FIELDS = ("code", "label", "description", "apps", "position", "is_default", "signup_visible", "is_active")


def _require_platform(actor: Any) -> None:
    from skeleton.accounts.platform import require_platform_context

    require_platform_context(actor)


def _clean(data: Mapping[str, Any], existing: Any) -> dict[str, Any]:
    unknown = set(data) - set(FIELDS)
    if unknown:
        raise ValidationError({name: gettext("Campo não permitido.") for name in sorted(unknown)})
    values = {name: data.get(name, getattr(existing, name, None)) for name in FIELDS}
    code = values["code"] or ""
    if existing is not None and code != existing.code:
        raise ValidationError({"code": gettext("Não pode ser alterado.")})
    if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", code):
        raise ValidationError({"code": gettext("Use letras minúsculas, dígitos e hífen.")})
    label = values["label"].strip() if isinstance(values["label"], str) else ""
    if not label or len(label) > 200:
        raise ValidationError({"label": gettext("Informe o nome (até 200 caracteres).")})
    description = values["description"] or ""
    if not isinstance(description, str) or len(description) > 2000:
        raise ValidationError({"description": gettext("Use até 2000 caracteres.")})
    apps = values["apps"] or []
    installed = {manifest.app_label for manifest in all_apps()}
    if not isinstance(apps, list | tuple) or not all(isinstance(label, str) for label in apps):
        raise ValidationError({"apps": gettext("Informe a lista de apps.")})
    if len(set(apps)) != len(apps):
        raise ValidationError({"apps": gettext("Uma app aparece mais de uma vez.")})
    missing = sorted(set(apps) - installed)
    if missing:
        raise ValidationError({"apps": gettext("Apps não instaladas: %(apps)s.") % {"apps": ", ".join(missing)}})
    position = values["position"] or 0
    if type(position) is not int or position < 0:
        raise ValidationError({"position": gettext("Use um inteiro positivo.")})
    return {
        "code": code,
        "label": label,
        "description": description.strip(),
        "apps": sorted(apps),
        "position": position,
        "is_default": bool(values["is_default"]),
        "signup_visible": bool(values["signup_visible"]),
        "is_active": values["is_active"] is not False,
    }


@transaction.atomic
def save_bundle(*, actor: Any, data: Mapping[str, Any], sysid: Any = None, expected_revision: int = 0) -> Any:
    """Create (``sysid=None``) or change a bundle with CAS; at most one default."""
    from .models import AppBundle

    _require_platform(actor)
    existing = None
    if sysid is not None:
        existing = AppBundle.objects.select_for_update().filter(pk=sysid).first()
        if existing is None or type(expected_revision) is not int or existing.revision != expected_revision:
            raise ConcurrentUpdate("Bundle changed.")
    values = _clean(data, existing)
    try:
        with transaction.atomic():
            if existing is None:
                row = AppBundle(**values)
                row.save()
                return row
            changed = {name for name, value in values.items() if getattr(existing, name) != value}
            for name in changed:
                setattr(existing, name, values[name])
            if changed:
                existing.save(update_fields=changed)
            return existing
    except IntegrityError:
        raise ValidationError({"code": gettext("Já existe um pacote com este código ou outro padrão.")}) from None


def bundle_of(organization_id: Any) -> Any:
    from .models import OrganizationBundle

    row = OrganizationBundle.objects.select_related("bundle").filter(bundled_organization_id=organization_id).first()
    return row.bundle if row is not None else None


def _ordered(labels: set[str]) -> list[str]:
    """Labels with their dependencies first."""
    result: list[str] = []
    for label in sorted(labels):
        for item in [*required_labels(label), label]:
            if item not in result:
                result.append(item)
    return result


def _grant_without_platform(label: str, organization: Any) -> None:
    """Offer a ``selected`` app to the organization from infrastructure (the signup, #715)."""
    from .models import AppAvailability, AppOrganizationGrant

    row = AppAvailability.objects.filter(app_label=label).first()
    if row is None:
        raise ValidationError({"apps": gettext("Defina a disponibilidade da app %(app)s antes.") % {"app": label}})
    if row.mode != AppAvailability.Mode.SELECTED:
        return
    grant = AppOrganizationGrant.all_objects.filter(availability=row, granted_organization=organization).first()
    if grant is None:
        AppOrganizationGrant(availability=row, granted_organization=organization).save()
    elif grant.deleted_at is not None:
        grant.restore()


def apply_bundle(organization: Any, bundle: Any, *, actor: Any = None, lock_apps: bool = False) -> Any:
    """Activate the bundle's apps, deactivate the previous bundle's others, record the bundle.

    With ``actor`` (the platform administrator), apps are assigned by ``assign_app`` and
    withdrawn by ``unassign_app`` (#699) and may be locked (#701). Without it (the signup, in
    its own system context), the grants and activations are written directly.
    """
    from skeleton.common.exceptions import ApplicationNotEntitled

    from .applications import (
        _activate_by_infrastructure,
        _deactivate_by_infrastructure,
        application_states,
        assign_app,
        unassign_app,
    )

    previous = bundle_of(organization.pk)
    keep = set(bundle.apps)
    leaving = set(previous.apps) - keep if previous is not None else set()
    try:
        if actor is not None:
            with system_context(reason="platform:organization-inspect", organization_id=organization.pk):
                effective = {label for label, state in application_states().items() if state["effective"]}
            for label in sorted(keep - effective):
                assign_app(label, user=actor, organization_id=organization.pk)
            for label in reversed(_ordered(leaving)):
                if label in leaving:
                    unassign_app(label, user=actor, organization_id=organization.pk)
        else:
            for label in _ordered(keep):
                _grant_without_platform(label, organization)
            for label in _ordered(keep):
                _activate_by_infrastructure(label)
            for label in reversed(_ordered(leaving)):
                if label in leaving:
                    _deactivate_by_infrastructure(label)
    except ApplicationNotEntitled as refused:
        message = gettext("App paga: use a cobrança da organização.")
        raise ApplicationNotEntitled({"bundle": ValidationError(message, code="app_not_entitled")}) from refused
    row = record_bundle(organization, bundle)
    if actor is not None and lock_apps:
        _lock_apps(actor, organization, keep, leaving)
    return row


def record_bundle(organization: Any, bundle: Any) -> Any:
    """Record the organization's bundle (restoring a removed row) without touching its apps."""
    from .models import OrganizationBundle

    row = OrganizationBundle.all_objects.filter(bundled_organization=organization).first()
    if row is None:
        row = OrganizationBundle(bundled_organization=organization, bundle=bundle)
        row.save()
        return row
    if row.deleted_at is not None:
        row.restore()
    if row.bundle_id != bundle.pk:
        row.bundle = bundle
        row.save(update_fields={"bundle"})
    return row


def _lock_apps(actor: Any, organization: Any, keep: set[str], leaving: set[str]) -> None:
    from skeleton.tenancy.models import OrganizationSettingLock
    from skeleton.tenancy.setting_locks import lock_setting, unlock_setting

    locks = {row.key: row for row in OrganizationSettingLock.objects.filter(locked_organization=organization)}
    reason = gettext("Pacote de apps do nível contratado.")
    for label in sorted(keep):
        key = f"apps.{label}.enabled"
        current = locks.get(key)
        lock_setting(
            actor=actor,
            organization=organization,
            key=key,
            value=True,
            reason=reason,
            expected_revision=current.revision if current is not None else 0,
        )
    for label in sorted(leaving):
        current = locks.get(f"apps.{label}.enabled")
        if current is not None:
            unlock_setting(actor=actor, organization=organization, key=current.key, expected_revision=current.revision)


@transaction.atomic
def assign_bundle(*, actor: Any, organization_id: Any, bundle: Any, lock_apps: bool = False) -> Any:
    """The platform gives an organization a bundle (or changes it), in one transaction."""
    from skeleton.tenancy.models import Organization

    _require_platform(actor)
    if not bundle.is_active or bundle.deleted_at is not None:
        raise ValidationError({"bundle": gettext("Escolha um pacote ativo.")})
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        raise ValidationError({"organization": gettext("Organização indisponível.")})
    return apply_bundle(organization, bundle, actor=actor, lock_apps=lock_apps)


@dataclass(frozen=True)
class BundlePreview:
    entering: tuple[str, ...]
    leaving: tuple[str, ...]
    members_before: int | None
    members_after: int | None
    members_used: int

    @property
    def over_limit(self) -> bool:
        return self.members_after is not None and self.members_used > self.members_after


def bundle_change_preview(*, actor: Any, organization_id: Any, bundle: Any) -> BundlePreview:
    """What a bundle change does: apps in and out and the member limit before and after (no write)."""
    from skeleton.access.administration import read_only_inspection
    from skeleton.accounts.platform import require_context_actor, require_platform_admin
    from skeleton.common.limits import combine

    from .applications import application_states
    from .limits import MEMBERS, limit_status, live_override
    from .models import AppAvailability

    require_platform_admin(actor)
    require_context_actor(actor)
    previous = bundle_of(organization_id)
    with (
        system_context(reason="platform:organization-inspect", organization_id=organization_id),
        read_only_inspection("default"),
    ):
        effective = {label for label, state in application_states().items() if state["effective"]}
        status = limit_status(MEMBERS)
    leaving = (set(previous.apps) - set(bundle.apps)) if previous is not None else set()
    after_apps = (effective - leaving) | set(bundle.apps)
    others = [item.amount for item in status.contributions if not item.source.startswith("app:")]
    rows = dict(AppAvailability.objects.filter(app_label__in=after_apps).values_list("app_label", "limits"))
    adds = [int((rows.get(label) or {}).get(MEMBERS, 0) or 0) for label in sorted(after_apps)]
    override = live_override(organization_id, MEMBERS)
    after = (
        override.value
        if override is not None and override.mode == "fixed"
        else combine(status.base, [*others, *(amount for amount in adds if amount)])
    )
    return BundlePreview(
        entering=tuple(sorted(set(bundle.apps) - effective)),
        leaving=tuple(sorted(leaving & effective)),
        members_before=status.limit,
        members_after=after,
        members_used=status.used,
    )
