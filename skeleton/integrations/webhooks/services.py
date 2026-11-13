"""Webhook endpoints of an organization: register, change, rotate the secret and remove (#63).

Every change requires ``integrations.manage`` under ``organization_lock``. The URL is
validated by the SSRF-safe client when saved (and again on every delivery, #64), the
principal must be an active service account of the same organization, and only event types
declared with ``webhook=True`` can be subscribed. The signing secret is returned once, by
creation and rotation, and is never readable again.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

SECRET_PREFIX = "whsec_"  # noqa: S105 - public prefix of generated secrets
ROTATION_OVERLAP = timedelta(hours=24)
FIELDS = ("name", "url", "principal", "event_types", "is_enabled")


@dataclass(frozen=True)
class IssuedSecret:
    """The plaintext signing secret, available only in the response that created it."""

    endpoint: Any
    secret: str

    def __repr__(self) -> str:
        return f"IssuedSecret(endpoint={self.endpoint.pk}, secret=<hidden>)"


def _new_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def _clean_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 150:
        raise ValidationError({"name": gettext("Informe um nome de até 150 caracteres.")})
    return value.strip()


def _clean_url(value: Any) -> str:
    from skeleton.common.outbound_http import OutboundRequestRefused, validate_url

    if not isinstance(value, str) or len(value) > 2000:
        raise ValidationError({"url": gettext("Informe uma URL HTTPS de até 2000 caracteres.")})
    try:
        validate_url(value)
    except OutboundRequestRefused as exc:
        raise ValidationError({"url": ValidationError(str(exc), code=exc.code)}) from None
    return value


def _clean_event_types(value: Any) -> list[str]:
    from skeleton.events.registry import get_event_type

    if not isinstance(value, list) or not value or not all(isinstance(code, str) for code in value):
        raise ValidationError({"event_types": gettext("Escolha ao menos um tipo de evento.")})
    for code in value:
        try:
            exposed = get_event_type(code).webhook
        except KeyError:
            exposed = False
        if not exposed:
            raise ValidationError(
                {
                    "event_types": ValidationError(
                        gettext("Tipo de evento não disponível para webhooks: %(code)s.") % {"code": code},
                        code="event_type",
                    )
                }
            )
    return sorted(set(value))


def _clean_principal(value: Any) -> Any:
    from skeleton.accounts.models import AccountType, User
    from skeleton.tenancy.context import current_context
    from skeleton.tenancy.models import Membership

    organization_id = current_context().organization_id
    pk = getattr(value, "pk", value)
    principal = (
        User.objects.filter(
            pk=pk, account_type=AccountType.SERVICE, service_organization_id=organization_id, is_active=True
        ).first()
        if pk
        else None
    )
    if principal is None or not Membership.objects.filter(user=principal, is_active=True).exists():
        raise ValidationError(
            {
                "principal": ValidationError(
                    gettext("Use uma conta de serviço ativa desta organização."), code="principal"
                )
            }
        )
    return principal


def available_event_types() -> list[str]:
    from skeleton.events.registry import all_event_types

    return [event_type.code for event_type in all_event_types() if event_type.webhook]


def clean_values(data: dict[str, Any], *, partial: bool, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    unknown = set(data) - set(FIELDS)
    if unknown:
        raise ValidationError({name: gettext("Campo não permitido.") for name in sorted(unknown)})
    cleaners = {
        "name": _clean_name,
        "url": _clean_url,
        "principal": _clean_principal,
        "event_types": _clean_event_types,
        "is_enabled": lambda value: value,
    }
    if not partial:
        missing = [name for name in ("name", "url", "principal", "event_types") if name not in data]
        if missing:
            raise ValidationError({name: gettext("Campo obrigatório.") for name in missing})
    if "is_enabled" in data and type(data["is_enabled"]) is not bool:
        raise ValidationError({"is_enabled": gettext("Use true ou false.")})
    return {name: cleaners[name](value) for name, value in data.items() if name not in skip}


def _lock(actor: Any) -> Any:
    from skeleton.access.capabilities import INTEGRATIONS_MANAGE
    from skeleton.access.services import organization_lock

    return organization_lock(actor, capability=INTEGRATIONS_MANAGE)


CONSECUTIVE_FAILURES = "consecutive_failures"
PRINCIPAL_DEACTIVATED = "principal_deactivated"


def disable_endpoint(endpoint: Any, *, reason: str, now: Any = None, failures: int | None = None) -> None:
    """Turn an endpoint off for ``reason`` and tell the organization administrators (#313).

    The caller already holds the endpoint row lock (``select_for_update``) in a tenant or
    system context of the endpoint's organization.
    """
    from skeleton.access.models import Role
    from skeleton.notifications.services import Recipients, notify

    from .delivery import DISABLED_NOTIFICATION

    now = now or timezone.now()
    endpoint.is_enabled = False
    endpoint.disabled_at = now
    endpoint.disabled_reason = reason
    endpoint.save(update_fields={"is_enabled", "disabled_at", "disabled_reason"})
    admins = tuple(Role.objects.filter(code="organization_admin", is_builtin=True).values_list("pk", flat=True))
    payload = {"endpoint": endpoint.name, "reason": reason}
    if failures is not None:
        payload["failures"] = str(failures)
    notify(
        DISABLED_NOTIFICATION,
        recipients=Recipients(roles=admins),
        payload=payload,
        dedupe_key=f"webhook-disabled:{endpoint.pk}:{now.isoformat()}",
    )


def disable_endpoints_of_principal(principal_id: Any, *, reason: str = PRINCIPAL_DEACTIVATED) -> list[Any]:
    """Disable every enabled endpoint whose deliveries act as ``principal_id``.

    Call it inside the organization's context, under ``organization_lock`` when an actor is
    changing the account. Returns the endpoints it disabled.
    """
    from .models import WebhookEndpoint

    disabled = []
    endpoints = WebhookEndpoint.objects.select_for_update().filter(principal_id=principal_id, is_enabled=True)
    for endpoint in endpoints.order_by("sysid"):
        disable_endpoint(endpoint, reason=reason)
        disabled.append(endpoint)
    return disabled


def _require_manage(actor: Any) -> None:
    """The denial ``_lock`` would give, before any outbound name lookup runs for the caller."""
    from skeleton.access.capabilities import INTEGRATIONS_MANAGE, require_capability

    require_capability(actor, INTEGRATIONS_MANAGE)


def _locked_endpoint(endpoint: Any, expected_revision: int | None) -> Any:
    from .models import WebhookEndpoint

    locked = WebhookEndpoint.objects.select_for_update().filter(pk=endpoint.pk).first()
    if locked is None:
        raise WebhookEndpoint.DoesNotExist()
    if expected_revision is not None and locked.revision != expected_revision:
        raise ConcurrentUpdate("The webhook endpoint changed.")
    return locked


def create_endpoint(*, actor: Any, data: dict[str, Any]) -> IssuedSecret:
    from .models import WebhookEndpoint

    _require_manage(actor)
    # The URL check resolves a name: it runs before the organization lock, never under it (#295).
    values = clean_values(data, partial=False, skip=("principal",))
    with _lock(actor) as organization:
        values["principal"] = _clean_principal(data["principal"])
        secret = _new_secret()
        endpoint = WebhookEndpoint(organization=organization, secret_ciphertext=secret, **values)
        endpoint.save()
        return IssuedSecret(endpoint, secret)


def update_endpoint(endpoint: Any, *, actor: Any, data: dict[str, Any], expected_revision: int) -> Any:
    _require_manage(actor)
    values = clean_values(data, partial=True, skip=("principal",))
    with _lock(actor):
        if "principal" in data:
            # The principal depends on the memberships: it is checked under the lock.
            values["principal"] = _clean_principal(data["principal"])
        locked = _locked_endpoint(endpoint, expected_revision)
        changed = {name for name, value in values.items() if getattr(locked, name) != value}
        for name in changed:
            setattr(locked, name, values[name])
        if "is_enabled" in changed and locked.is_enabled:
            # Re-enabling by hand clears an automatic disablement (#64).
            locked.disabled_at, locked.disabled_reason = None, ""
            changed |= {"disabled_at", "disabled_reason"}
        if changed:
            locked.save(update_fields=changed)
        if "principal" in changed:
            # Open deliveries are kept and re-enqueued as the new principal (#313).
            from .delivery import enqueue_attempt
            from .models import WebhookDelivery

            open_deliveries = WebhookDelivery.objects.filter(endpoint=locked, state__in=("pending", "retrying"))
            for delivery in open_deliveries.order_by("created_at", "sysid"):
                enqueue_attempt(delivery, locked.principal_id, run_after=delivery.next_attempt_at)
        return locked


def rotate_secret(endpoint: Any, *, actor: Any, expected_revision: int) -> IssuedSecret:
    """New secret now; the previous one keeps signing deliveries for ``ROTATION_OVERLAP``."""
    with _lock(actor):
        locked = _locked_endpoint(endpoint, expected_revision)
        secret = _new_secret()
        locked.previous_secret_ciphertext = locked.secret_ciphertext
        locked.previous_valid_until = timezone.now() + ROTATION_OVERLAP
        locked.secret_ciphertext = secret
        locked.save(update_fields={"previous_secret_ciphertext", "previous_valid_until", "secret_ciphertext"})
        return IssuedSecret(locked, secret)


def delete_endpoint(endpoint: Any, *, actor: Any, expected_revision: int) -> None:
    with _lock(actor):
        _locked_endpoint(endpoint, expected_revision).delete()


def signing_secrets(endpoint: Any, *, now: Any = None) -> list[str]:
    """The current secret, plus the previous one while its overlap lasts (deliveries sign with both)."""
    now = now or timezone.now()
    result = [endpoint.secret_ciphertext.reveal()]
    if (
        endpoint.previous_secret_ciphertext is not None
        and endpoint.previous_valid_until
        and now < endpoint.previous_valid_until
    ):
        result.append(endpoint.previous_secret_ciphertext.reveal())
    return result
