"""What every new organization receives (#703): rules of the platform per setting.

A rule names a lockable setting (#701) or a platform field of an app (#702) and says how a new
organization gets it: ``default`` (the rule's value) or ``required`` (the value the platform
gives when it creates the organization). With ``locked``, the setting is also locked with the
rule's reason. ``create_organization`` applies the rules in its own transaction, so a missing
required value or an invalid one undoes the whole creation. A ``default`` rule may also be
applied to the existing organizations by one job per organization, each in its own transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.common.logging import security_event
from skeleton.tenancy.context import system_context

APPLY_JOB = "tenancy.apply_provisioning_rule"


@dataclass(frozen=True)
class RuleTarget:
    """What a rule may set: a lockable setting or a platform field of an app."""

    key: str
    label: str
    group: str
    kind: str
    normalize: Callable[[Any], Any]
    apply: Callable[[Any], None]
    lockable: bool  # A platform field is the platform's already: there is nothing to lock.


def rule_targets() -> dict[str, RuleTarget]:
    """Every key a rule may name now: lockable settings plus platform fields of installed apps."""
    from skeleton.system.app_registry import all_apps
    from skeleton.system.applications import platform_fields
    from skeleton.system.setting_locks import write_configuration_value

    from .setting_locks import lockable_settings

    targets = {
        key: RuleTarget(key, setting.label, setting.group, setting.kind, setting.normalize, setting.apply, True)
        for key, setting in lockable_settings().items()
        if setting.kind != "frozen"
    }
    for manifest in all_apps():
        for field in platform_fields(manifest):
            key = f"apps.{manifest.app_label}.{field.name}"

            def normalize(value: Any, field: Any = field) -> Any:
                if field.value_type is int and isinstance(value, str):
                    try:
                        value = int(value)
                    except ValueError:
                        raise ValidationError({"value": gettext("Use um número inteiro.")}) from None
                if type(value) is not field.value_type or (isinstance(value, str) and len(value) > field.max_length):
                    raise ValidationError({"value": gettext("Valor inválido para %(name)s.") % {"name": field.name}})
                return value

            def apply(value: Any, label: str = manifest.app_label, name: str = field.name) -> None:
                write_configuration_value(label, name, value)

            kind = {bool: "bool", int: "int"}.get(field.value_type, "str")
            group = gettext("App %(name)s") % {"name": manifest.label}
            targets[key] = RuleTarget(key, field.label or field.name, group, kind, normalize, apply, False)
    return dict(sorted(targets.items()))


def rules() -> tuple[Any, ...]:
    from .models import OrganizationProvisioningRule

    return tuple(OrganizationProvisioningRule.objects.order_by("key"))


def required_rules() -> tuple[Any, ...]:
    """The live ``required`` rules whose key still exists (the creation form asks for them)."""
    targets = rule_targets()
    return tuple(rule for rule in rules() if rule.mode == "required" and rule.key in targets)


def _validated(key: str, mode: str, value: Any, locked: Any, reason: Any) -> tuple[Any, bool, str]:
    target = rule_targets().get(key)
    if target is None:
        raise ValidationError({"key": gettext("Configuração desconhecida: %(key)s.") % {"key": key}})
    if mode not in ("default", "required"):
        raise ValidationError({"mode": gettext("Use default ou required.")})
    if type(locked) is not bool:
        raise ValidationError({"locked": gettext("Use verdadeiro ou falso.")})
    reason = reason.strip() if isinstance(reason, str) else ""
    if len(reason) > 500 or (locked and not reason):
        raise ValidationError({"reason": gettext("Informe o motivo da trava (até 500 caracteres).")})
    if mode == "default":
        if value is None:
            raise ValidationError({"value": gettext("Informe o valor inicial.")})
        value = target.normalize(value)
    else:
        value = None
    return value, locked and target.lockable, reason


@transaction.atomic
def save_rule(
    *, actor: Any, key: str, mode: str, value: Any, locked: bool, reason: str = "", expected_revision: int = 0
) -> Any:
    """Create, change or restore the rule of ``key`` with CAS (0: create or restore a removed one)."""
    from skeleton.accounts.platform import require_platform_context

    from .models import OrganizationProvisioningRule

    require_platform_context(actor)
    value, locked, reason = _validated(key, mode, value, locked, reason)
    if type(expected_revision) is not int:
        raise ConcurrentUpdate("Provisioning rule changed.")
    row = OrganizationProvisioningRule.all_objects.select_for_update().filter(key=key).first()
    live = row is not None and row.deleted_at is None
    if (live and row.revision != expected_revision) or (not live and expected_revision != 0):
        raise ConcurrentUpdate("Provisioning rule changed.")
    wanted = {"mode": mode, "value": value, "locked": locked, "reason": reason}
    if row is None:
        row = OrganizationProvisioningRule(key=key, **wanted)
        row.save()
        return row
    if not live:
        row.restore()
    changed = {name for name, new in wanted.items() if getattr(row, name) != new}
    for name in changed:
        setattr(row, name, wanted[name])
    if changed:
        row.save(update_fields=changed)
    return row


@transaction.atomic
def remove_rule(*, actor: Any, key: str, expected_revision: int) -> None:
    """Remove a rule (soft delete): organizations created later no longer receive it."""
    from skeleton.accounts.platform import require_platform_context

    from .models import OrganizationProvisioningRule

    require_platform_context(actor)
    row = OrganizationProvisioningRule.objects.select_for_update().filter(key=key).first()
    if row is None or type(expected_revision) is not int or row.revision != expected_revision:
        raise ConcurrentUpdate("Provisioning rule changed.")
    row.delete()


def _lock(organization: Any, key: str, value: Any, reason: str) -> None:
    """Lock ``key`` with ``value`` as the platform does (#701), from inside the organization's context."""
    from .models import OrganizationSettingLock
    from .setting_locks import _forget

    row = OrganizationSettingLock.all_objects.filter(locked_organization_id=organization.pk, key=key).first()
    if row is None:
        OrganizationSettingLock(locked_organization=organization, key=key, value=value, reason=reason).save()
    elif row.deleted_at is not None:
        row.restore()
        row.value, row.reason = value, reason
        row.save(update_fields={"value", "reason"})
    _forget()


def apply_provisioning(organization: Any, *, provided: Mapping[str, Any] | None = None) -> None:
    """Apply every rule to a new organization (in its bootstrap transaction).

    ``provided`` carries the values of the ``required`` rules (by key). A missing or invalid
    value raises ``ValidationError`` (``settings.<key>``) and the caller's transaction undoes
    the creation. A rule whose key no longer exists (an uninstalled app) is skipped and logged.
    """
    provided = dict(provided or {})
    targets = rule_targets()
    unknown = set(provided) - {rule.key for rule in rules() if rule.mode == "required"}
    if unknown:
        raise ValidationError({f"settings.{key}": gettext("Configuração não pedida.") for key in sorted(unknown)})
    for rule in rules():
        target = targets.get(rule.key)
        if target is None:
            security_event("provisioning.rule_skipped", reason_code="unknown_key", record_sysid=str(rule.pk))
            continue
        if rule.mode == "required":
            if provided.get(rule.key) in (None, ""):
                raise ValidationError({f"settings.{rule.key}": gettext("Obrigatório.")})
            try:
                value = target.normalize(provided[rule.key])
            except ValidationError as exc:
                raise ValidationError({f"settings.{rule.key}": exc.messages}) from None
        else:
            value = rule.value
        with system_context(reason="organization:bootstrap", organization_id=organization.pk):
            target.apply(value)
            if rule.locked and target.lockable:
                _lock(organization, rule.key, value, rule.reason)


def apply_rule_in_organization(*, rule_id: Any, **_: Any) -> str:
    """The job (system context of one organization): apply a ``default`` rule to it.

    An organization where the key is already locked is left as it is.
    """
    from skeleton.tenancy.context import current_context

    from .models import Organization, OrganizationProvisioningRule, OrganizationSettingLock

    organization_id = current_context().organization_id
    rule = OrganizationProvisioningRule.objects.filter(pk=rule_id, mode="default").first()
    target = rule_targets().get(rule.key) if rule is not None else None
    if rule is None or target is None:
        return "skipped"
    if OrganizationSettingLock.objects.filter(locked_organization_id=organization_id, key=rule.key).exists():
        return "locked"
    organization = Organization.objects.get(pk=organization_id)
    target.apply(rule.value)
    if rule.locked and target.lockable:
        _lock(organization, rule.key, rule.value, rule.reason)
    return "applied"


def enqueue_apply(*, actor: Any, rule: Any) -> int:
    """Queue one job per active organization to apply a ``default`` rule there (platform only).

    Each job runs in its own transaction, in the system context of its organization. Returns
    the number of organizations queued; the same rule revision is never queued twice.
    """
    from skeleton.accounts.platform import require_platform_context
    from skeleton.jobs.services import enqueue

    from .models import Organization

    require_platform_context(actor)
    if rule.mode != "default":
        raise ValidationError({"mode": gettext("Só uma regra de valor inicial se aplica às organizações existentes.")})
    organizations = list(Organization.objects.filter(is_active=True).values_list("pk", flat=True))
    for organization_id in organizations:
        enqueue(
            APPLY_JOB,
            organization_id=organization_id,
            payload={"rule_id": rule.pk},
            idempotency_key=f"provisioning:{rule.pk}:{rule.revision}:{organization_id}",
        )
    return len(organizations)


def register_provisioning_jobs() -> None:
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(
            APPLY_JOB,
            apply_rule_in_organization,
            {"rule_id": "uuid"},
            run_as="system",
            max_attempts=3,
            owner_app="sk_tenancy",
        )
    )
