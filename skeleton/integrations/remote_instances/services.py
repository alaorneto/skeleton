"""Connections of a central instance to remote ones (#712): only the platform administrator writes.

Saving validates the base URL and encrypts the token (an empty token in an edit keeps the
stored one); testing calls ``instance/`` of the remote and pins its ``instance_id`` the first
time. Granting names the one organization whose app may read the connection.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

from .client import RemoteInstanceError, get, validate_base_url
from .models import REMOTE_INSTANCE_KEYS


def _require_platform(actor: Any) -> None:
    from skeleton.accounts.platform import require_platform_context

    require_platform_context(actor)


@transaction.atomic
def save_connection(
    *,
    actor: Any,
    label: str,
    base_url: str,
    token: str = "",
    granted_organization: Any = None,
    sysid: Any = None,
    expected_revision: int = 0,
) -> Any:
    from .models import RemoteInstanceConnection

    _require_platform(actor)
    label = label.strip() if isinstance(label, str) else ""
    if not label or len(label) > 200:
        raise ValidationError({"label": gettext("Informe o nome (até 200 caracteres).")})
    base_url = validate_base_url(base_url)
    if not getattr(settings, REMOTE_INSTANCE_KEYS, None):
        raise ValidationError({"token": gettext("Configure REMOTE_INSTANCE_ENCRYPTION_KEYS.")})
    if not isinstance(token, str):
        raise ValidationError({"token": gettext("Use um texto.")})
    if sysid is None:
        if not token.strip():
            raise ValidationError({"token": gettext("Informe o token de plataforma da instância remota.")})
        row = RemoteInstanceConnection(
            label=label, base_url=base_url, token=token.strip(), granted_organization=granted_organization
        )
        row.save()
        return row
    row = RemoteInstanceConnection.objects.select_for_update().filter(pk=sysid).first()
    if row is None or type(expected_revision) is not int or row.revision != expected_revision:
        raise ConcurrentUpdate("Remote connection changed.")
    changed = set()
    if row.base_url != base_url:
        row.base_url, row.expected_instance_id = base_url, None  # Another base: pin again at the next test.
        changed |= {"base_url", "expected_instance_id"}
    if row.label != label:
        row.label = label
        changed.add("label")
    if row.granted_organization_id != getattr(granted_organization, "pk", None):
        row.granted_organization = granted_organization
        changed.add("granted_organization")
    if token.strip():
        row.token = token.strip()
        changed.add("token")
    if changed:
        row.save(update_fields=changed)
    return row


@transaction.atomic
def deactivate_connection(*, actor: Any, sysid: Any, expected_revision: int) -> None:
    from .models import RemoteInstanceConnection

    _require_platform(actor)
    row = RemoteInstanceConnection.objects.select_for_update().filter(pk=sysid).first()
    if row is None or type(expected_revision) is not int or row.revision != expected_revision:
        raise ConcurrentUpdate("Remote connection changed.")
    row.is_active = False
    row.save(update_fields={"is_active"})


@transaction.atomic
def test_connection(*, actor: Any, sysid: Any) -> str:
    """Call ``instance/`` of the remote; pins its id the first time; returns ``""`` or an error code."""
    from .models import RemoteInstanceConnection

    _require_platform(actor)
    row = RemoteInstanceConnection.objects.select_for_update().filter(pk=sysid).first()
    if row is None:
        raise ConcurrentUpdate("Remote connection changed.")
    code = ""
    try:
        data = get(row, "instance/")
        if row.expected_instance_id is None:
            row.expected_instance_id = UUID(str(data.get("instance_id")))
    except RemoteInstanceError as exc:
        code = exc.code
    except (ValueError, TypeError):
        code = "remote_invalid_response"
    row.last_checked_at = timezone.now()
    row.last_error_code = code
    row.save(update_fields={"expected_instance_id", "last_checked_at", "last_error_code"})
    return code
