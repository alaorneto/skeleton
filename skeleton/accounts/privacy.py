"""The data subject's own data (#190): a ZIP of JSON with what the core keeps about the person.

``export_my_data`` reads only the person's own rows: the identity, the profile and the API
token metadata in a request context of the person, then each organization of the person
through ``for_each_organization`` (one read-only tenant context each, the membership checked):
the membership, the notifications received and the metadata of the attachments the person
uploaded. Never another person's data, never business records of the organization (their
controller is the organization), never secrets (token digests, storage keys).

The export is built in the request and returned at once, as an attachment with ``no-store``:
there is no stored file to protect or to expire.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from django.utils import timezone

FORMAT = 1


def _json(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def _identity(user: Any) -> dict[str, Any]:
    from skeleton.accounts.models import User, UserProfile
    from skeleton.integrations.tokens.services import personal_tokens
    from skeleton.tenancy.context import request_context

    with request_context(actor=user, request_id=uuid4(), source="privacy:export"):
        person = User.objects.get(pk=user.pk)
        profile = UserProfile.objects.filter(user=person).first()
        tokens = [
            {
                "prefix": token.prefix,
                "name": token.name,
                "kind": token.kind,
                "scopes": list(token.scopes),
                "created_at": token.created_at,
                "expires_at": token.expires_at,
                "revoked_at": token.revoked_at,
            }
            for token in personal_tokens(person)
        ]
    return {
        "identity": {
            "sysid": person.pk,
            "email": person.email,
            "first_name": person.first_name,
            "last_name": person.last_name,
            "created_at": person.created_at,
            "last_login": person.last_login,
            "is_active": person.is_active,
        },
        "profile": None
        if profile is None
        else {"display_name": profile.display_name, "language": profile.language, "timezone": profile.timezone},
        "api_tokens": tokens,
    }


def _organization_part(user: Any):
    def collect(organization_id: Any) -> dict[str, Any]:
        from skeleton.files.models import Attachment
        from skeleton.notifications.models import NotificationRecipient
        from skeleton.tenancy.models import Membership

        membership = Membership.objects.filter(user=user).first()
        notifications = [
            {
                "type_code": row.type_code,
                "created_at": row.created_at,
                "read_at": row.read_at,
                "target_table": row.target_table_key,
                "target_sysid": row.target_sysid,
                "snapshot": dict(row.snapshot),
            }
            for row in NotificationRecipient.objects.filter(user=user).order_by("created_at")[:5000]
        ]
        attachments = [
            {
                "filename": row.filename,
                "content_type": row.content_type,
                "size": row.size,
                "created_at": row.created_at,
                "deleted_at": row.deleted_at,
                "target_sysid": row.target_sysid,
            }
            for row in Attachment.objects.with_deleted().filter(created_by=user.pk).order_by("created_at")[:5000]
        ]
        return {
            "membership": None
            if membership is None
            else {"created_at": membership.created_at, "is_active": membership.is_active},
            "notifications": notifications,
            "attachments_uploaded": attachments,
        }

    return collect


def export_my_data(user: Any) -> bytes:
    """The ZIP with ``dados.json`` (call outside any context: an ``aggregate_route`` view)."""
    from skeleton.tenancy.aggregate import for_each_organization

    data = {"format": FORMAT, "generated_at": timezone.now(), **_identity(user)}
    data["organizations"] = [
        {"sysid": result.organization_id, "name": result.organization_name, **result.value}
        for result in for_each_organization(user, _organization_part(user), source="privacy:export")
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dados.json", json.dumps(data, ensure_ascii=False, indent=2, default=_json))
    return buffer.getvalue()
