"""The platform's storage locations (#704) and the organization each one serves (#705).

A location is an S3 bucket (endpoint, region, prefix and credential) or, only in development
and tests, a directory under ``SKELETON_FILES_LOCATIONS_ROOT``. The secret key is encrypted
with ``FILES_STORAGE_ENCRYPTION_KEYS`` and never shown: an empty secret in an edit keeps the
stored one. Only the platform administrator writes locations and assignments; an organization
never sees which location holds its files.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
PREFIX = re.compile(r"[a-z0-9-]+(/[a-z0-9-]+)*\Z")
DIRECTORY = re.compile(r"[a-z0-9-]+(/[a-z0-9-]+)*\Z")
TEXT_FIELDS = (
    "code",
    "label",
    "backend",
    "bucket_name",
    "region_name",
    "endpoint_url",
    "addressing_style",
    "key_prefix",
    "access_key_id",
    "directory",
)
WRITE_FIELDS = (*TEXT_FIELDS, "secret_access_key", "is_default")


def filesystem_allowed() -> bool:
    """Directory locations only in development and tests: never a production bucket stand-in."""
    return bool(getattr(settings, "SKELETON_FILES_DIRECTORY_LOCATIONS", False))


def _clean(data: Mapping[str, Any], existing: Any) -> dict[str, Any]:
    from .models import STORAGE_KEYS, StorageLocation

    unknown = set(data) - set(WRITE_FIELDS)
    if unknown:
        raise ValidationError({name: gettext("Campo não permitido.") for name in sorted(unknown)})
    values: dict[str, Any] = {}
    for name in TEXT_FIELDS:
        value = data.get(name, getattr(existing, name, "") if existing is not None else "")
        if not isinstance(value, str):
            raise ValidationError({name: gettext("Use um texto.")})
        values[name] = value.strip()
    values["is_default"] = bool(data.get("is_default", getattr(existing, "is_default", False)))
    if existing is not None:
        for name in ("code", "backend"):
            if values[name] != getattr(existing, name):
                raise ValidationError({name: gettext("Não pode ser alterado.")})
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", values["code"]):
        raise ValidationError({"code": gettext("Use letras minúsculas, dígitos e hífen.")})
    if not values["label"]:
        raise ValidationError({"label": gettext("Informe o nome.")})
    if values["backend"] not in StorageLocation.Backend.values:
        raise ValidationError({"backend": gettext("Use s3 ou filesystem.")})
    secret = data.get("secret_access_key", "")
    if not isinstance(secret, str):
        raise ValidationError({"secret_access_key": gettext("Use um texto.")})
    if values["backend"] == StorageLocation.Backend.FILESYSTEM:
        if not filesystem_allowed():
            raise ValidationError({"backend": gettext("Diretório local só em desenvolvimento e testes.")})
        if not DIRECTORY.fullmatch(values["directory"]):
            raise ValidationError({"directory": gettext("Use um subdiretório: minúsculas, dígitos, hífen e /.")})
        for name in ("bucket_name", "region_name", "endpoint_url", "key_prefix", "access_key_id"):
            values[name] = ""
        values["addressing_style"] = "auto"
        return values
    values["directory"] = ""
    if not BUCKET.fullmatch(values["bucket_name"]) or ".." in values["bucket_name"]:
        raise ValidationError({"bucket_name": gettext("Nome de bucket inválido (3 a 63 minúsculas, dígitos, - e .).")})
    if values["key_prefix"] and not PREFIX.fullmatch(values["key_prefix"]):
        raise ValidationError({"key_prefix": gettext("Prefixo inválido: minúsculas, dígitos, hífen e /.")})
    values["addressing_style"] = values["addressing_style"] or "auto"
    if values["addressing_style"] not in ("auto", "path", "virtual"):
        raise ValidationError({"addressing_style": gettext("Use auto, path ou virtual.")})
    endpoint = values["endpoint_url"]
    if endpoint:
        from urllib.parse import urlsplit

        parts = urlsplit(endpoint)
        development = settings.DEBUG and getattr(settings, "SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE", False)
        if parts.scheme != "https" and not (development and parts.scheme == "http"):
            raise ValidationError({"endpoint_url": gettext("Use um endereço https://.")})
        if parts.username or parts.password or not parts.hostname or parts.query or parts.fragment:
            raise ValidationError(
                {"endpoint_url": gettext("Endereço inválido: sem credencial, consulta ou fragmento.")}
            )
    if not getattr(settings, STORAGE_KEYS, None):
        raise ValidationError({"secret_access_key": gettext("Configure FILES_STORAGE_ENCRYPTION_KEYS.")})
    if secret:
        values["secret_access_key"] = secret
    elif existing is None:
        values["secret_access_key"] = ""
    return values


@transaction.atomic
def save_location(*, actor: Any, data: Mapping[str, Any], sysid: Any = None, expected_revision: int = 0) -> Any:
    """Create (``sysid=None``) or edit a location with CAS; an empty secret keeps the stored one."""
    from django.db import IntegrityError

    from skeleton.accounts.platform import require_platform_context

    from .models import StorageLocation

    require_platform_context(actor)
    existing = None
    if sysid is not None:
        existing = StorageLocation.objects.select_for_update().filter(pk=sysid).first()
        if existing is None or type(expected_revision) is not int or existing.revision != expected_revision:
            raise ConcurrentUpdate("Storage location changed.")
    values = _clean(data, existing)
    if values["is_default"]:
        for other in StorageLocation.objects.select_for_update().filter(is_default=True).exclude(pk=sysid):
            other.is_default = False
            other.save(update_fields={"is_default"})
    try:
        with transaction.atomic():
            if existing is None:
                record = StorageLocation(**values)
                record.save()
            else:
                changed = {
                    name
                    for name, value in values.items()
                    if name == "secret_access_key" or getattr(existing, name) != value
                }
                for name in changed:
                    setattr(existing, name, values[name])
                if changed:
                    existing.save(update_fields=changed)
                record = existing
    except IntegrityError:
        raise ValidationError({"code": gettext("Já existe um local com este código.")}) from None
    forget_storages()
    return record


@transaction.atomic
def deactivate_location(*, actor: Any, sysid: Any, expected_revision: int) -> None:
    """Turn a location off; refused while an organization uses it (#705)."""
    from skeleton.accounts.platform import require_platform_context

    from .models import OrganizationStorage, StorageLocation

    require_platform_context(actor)
    record = StorageLocation.objects.select_for_update().filter(pk=sysid).first()
    if record is None or type(expected_revision) is not int or record.revision != expected_revision:
        raise ConcurrentUpdate("Storage location changed.")
    if OrganizationStorage.objects.filter(location=record).exists():
        raise ValidationError(
            {"is_active": ValidationError(gettext("Há organizações usando este local."), code="storage_in_use")}
        )
    record.is_active = False
    record.is_default = False
    record.save(update_fields={"is_active", "is_default"})
    forget_storages()


def _secret(location: Any) -> str:
    secret = location.secret_access_key
    return secret.reveal() if hasattr(secret, "reveal") else (secret or "")


def build_storage(location: Any) -> Any:
    """The Django storage of a location: private S3 (signed URLs, no public ACL) or a test directory."""
    if location.backend == "filesystem":
        from django.core.files.storage import FileSystemStorage

        root = Path(settings.SKELETON_FILES_LOCATIONS_ROOT) / location.directory
        return FileSystemStorage(location=str(root), base_url=None)
    from storages.backends.s3 import S3Storage

    options: dict[str, Any] = {
        "bucket_name": location.bucket_name,
        "region_name": location.region_name or None,
        "endpoint_url": location.endpoint_url or None,
        "default_acl": "private",
        "querystring_auth": True,
        "querystring_expire": 60,
        "file_overwrite": False,
        "location": location.key_prefix,
        "addressing_style": None if location.addressing_style == "auto" else location.addressing_style,
    }
    if location.access_key_id:
        options["access_key"] = location.access_key_id
        options["secret_key"] = _secret(location)
    return S3Storage(**options)


@dataclass(frozen=True)
class TestResult:
    ok: bool
    message: str


def test_location(*, actor: Any, sysid: Any) -> TestResult:
    """Check that the location answers (bucket reachable, or the directory writable); generic on failure."""
    from skeleton.accounts.platform import require_platform_context

    from .models import StorageLocation

    require_platform_context(actor)
    location = StorageLocation.objects.filter(pk=sysid).first()
    if location is None:
        raise ConcurrentUpdate("Storage location changed.")
    try:
        if location.backend == "filesystem":
            root = Path(settings.SKELETON_FILES_LOCATIONS_ROOT) / location.directory
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".skeleton-probe"
            probe.write_bytes(b"ok")
            probe.unlink()
        else:
            storage = build_storage(location)
            client = storage.connection.meta.client
            client.meta.config.connect_timeout = 10
            client.meta.config.read_timeout = 10
            client.head_bucket(Bucket=location.bucket_name)
    except Exception:  # noqa: BLE001 - the answer of the server is never echoed.
        return TestResult(False, gettext("O local não respondeu. Confira o endpoint, o bucket e a credencial."))
    return TestResult(True, gettext("Conexão bem-sucedida."))


# --------------------------------------------------------------------------- per organization (#705)

_cache: dict[Any, tuple[float, Any]] = {}
_storages: dict[tuple[Any, int], Any] = {}
TTL = 60.0


def forget_storages() -> None:
    """Forget the resolved locations in this process, after the commit of a write."""

    def clear() -> None:
        _cache.clear()
        _storages.clear()

    transaction.on_commit(clear)
    clear()


def location_for(organization_id: Any) -> Any:
    """The active location assigned to the organization, or ``None`` (the instance's storage).

    The assignment is a global row, readable in any context and in jobs. Resolved locations
    are cached per process for 60 s (several nodes do not share memory) and forgotten in this
    process when an assignment or a location is written.
    """
    import time

    from .models import OrganizationStorage

    now = time.monotonic()
    hit = _cache.get(organization_id)
    if hit is not None and now - hit[0] < TTL:
        return hit[1]
    row = (
        OrganizationStorage.objects.select_related("location")
        .filter(storage_organization_id=organization_id, location__is_active=True, location__deleted_at__isnull=True)
        .first()
    )
    location = row.location if row is not None else None
    _cache[organization_id] = (now, location)
    return location


def storage_of(location: Any) -> Any:
    key = (location.pk, location.revision)
    storage = _storages.get(key)
    if storage is None:
        storage = _storages[key] = build_storage(location)
    return storage


def assign_storage(*, actor: Any, organization: Any, location: Any) -> Any:
    """Point an organization at a location (``None``: the instance's storage).

    Refused with ``storage_in_use`` while the organization has files in its current storage:
    files are never moved between locations. An inactive location is refused.
    """
    from skeleton.accounts.platform import require_platform_context

    require_platform_context(actor)
    if location is not None and (not location.is_active or location.deleted_at is not None):
        raise ValidationError({"location": gettext("Escolha um local ativo.")})
    if organization_has_files(organization.pk):
        raise ValidationError(
            {
                "location": ValidationError(
                    gettext("A organização já tem arquivos no armazenamento atual."), code="storage_in_use"
                )
            }
        )
    return _assign(organization, location)


def _assign(organization: Any, location: Any) -> Any:
    from .models import OrganizationStorage

    row = OrganizationStorage.all_objects.select_for_update().filter(storage_organization=organization).first()
    if location is None:
        if row is not None and row.deleted_at is None:
            row.delete()
        forget_storages()
        return None
    if row is None:
        row = OrganizationStorage(storage_organization=organization, location=location)
        row.save()
    else:
        if row.deleted_at is not None:
            row.restore()
        if row.location_id != location.pk:
            row.location = location
            row.save(update_fields={"location"})
    forget_storages()
    return row


def organization_has_files(organization_id: Any) -> bool:
    """Live attachment bytes, counted usage or any object under the organization's prefixes."""
    from skeleton.access.administration import read_only_inspection
    from skeleton.tenancy.context import system_context

    from .models import Attachment, StorageUsage
    from .storage import list_prefix

    with (
        system_context(reason="platform:organization-inspect", organization_id=organization_id),
        read_only_inspection("default"),
    ):
        if Attachment.all_objects.exclude(state="purged").exists():
            return True
        if StorageUsage.objects.filter(bytes_used__gt=0).exists():
            return True
    base = f"org/{organization_id}"
    return any(list_prefix(prefix) for prefix in (base, f"{base}/branding", f"{base}/transfers"))


def assign_at_creation(organization: Any, location: Any = "default") -> None:
    """At creation (#706): the location chosen; ``"default"`` the active default; ``None`` the instance's."""
    from .models import StorageLocation

    if location == "default":
        location = StorageLocation.objects.filter(is_default=True, is_active=True).first()
    elif location is not None and (not location.is_active or location.deleted_at is not None):
        raise ValidationError({"storage_location": gettext("Escolha um local de armazenamento ativo.")})
    if location is not None:
        _assign(organization, location)
