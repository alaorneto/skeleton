"""RemoteInstanceService (#712): an app of a central instance reads the remote ones it was granted.

Every method makes a network call to the remote's platform API, so it runs only in jobs
(``actor=None`` in the ``system_context`` of the granted organization); inside an HTTP request it
answers ``Unavailable("remote_call_in_request")``. A connection of another organization or an
inactive one is ``NotFound``. The DTOs carry only the fields of the platform API, never the token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.utils.dateparse import parse_date, parse_datetime

from .base import ServiceAPI, service_method
from .errors import Conflict, NotFound, Unavailable


@dataclass(frozen=True, slots=True)
class RemoteConnectionInfo:
    sysid: UUID
    label: str
    base_url: str
    instance_id: UUID | None
    last_checked_at: datetime | None
    last_error_code: str


@dataclass(frozen=True, slots=True)
class RemoteInstanceInfo:
    instance_id: UUID
    name: str
    purpose: str
    product_name: str
    core_version: str | None
    billing_enabled: bool
    apps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RemoteOrganization:
    id: UUID
    name: str
    slug: str
    is_active: bool
    members_active: int
    members_limit: int | None
    apps: tuple[str, ...]
    bundle: str | None
    storage_used_bytes: int
    storage_quota_bytes: int | None
    billing_json: str  # The ``billing`` object of the platform API, as JSON ("{}" without the scope).
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RemoteOrganizationPage:
    items: tuple[RemoteOrganization, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class RemoteInvoice:
    id: UUID
    number: str | None
    status: str
    currency: str
    total: int
    paid: int
    issued_at: datetime | None
    due_at: datetime | None
    paid_at: datetime | None


@dataclass(frozen=True, slots=True)
class RemotePayment:
    id: UUID
    invoice_id: UUID | None
    method: str
    status: str
    amount: int
    currency: str
    created_at: datetime
    confirmed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RemoteMetric:
    day: date
    code: str
    currency: str
    value: int


def _moment(value: Any) -> datetime | None:
    return parse_datetime(value) if isinstance(value, str) else None


def _required_moment(value: Any) -> datetime:
    moment = _moment(value)
    if moment is None:
        raise ValueError("moment")
    return moment


def _organization(item: dict[str, Any]) -> RemoteOrganization:
    members = item.get("members") or {}
    storage = item.get("storage") or {}
    moment = _moment(item.get("updated_at"))
    if moment is None:
        raise ValueError("updated_at")
    return RemoteOrganization(
        id=UUID(str(item["id"])),
        name=str(item.get("name", "")),
        slug=str(item.get("slug", "")),
        is_active=bool(item.get("is_active")),
        members_active=int(members.get("active") or 0),
        members_limit=members.get("limit"),
        apps=tuple(str(label) for label in item.get("apps") or ()),
        bundle=item.get("bundle"),
        storage_used_bytes=int(storage.get("used_bytes") or 0),
        storage_quota_bytes=storage.get("quota_bytes"),
        billing_json=json.dumps(item.get("billing") or {}, sort_keys=True),
        updated_at=moment,
    )


def _connection(connection_id: Any) -> Any:
    from skeleton.integrations.remote_instances.models import RemoteInstanceConnection
    from skeleton.tenancy.context import current_context

    context = current_context()
    if not context.system:
        raise Unavailable("remote_call_in_request")
    row = RemoteInstanceConnection.objects.filter(
        pk=connection_id, is_active=True, granted_organization_id=context.organization_id
    ).first()
    if row is None:
        raise NotFound()
    return row


def _get(connection: Any, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from skeleton.integrations.remote_instances.client import RemoteInstanceError, get

    try:
        return get(connection, path, params)
    except RemoteInstanceError as exc:
        if exc.code == "remote_not_found":
            raise NotFound() from None
        raise Conflict(exc.code) from None


class RemoteInstanceService(ServiceAPI):
    """Read the remote instances granted to the organization of the context (jobs only)."""

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def connections(self, *, actor: AbstractBaseUser | None) -> tuple[RemoteConnectionInfo, ...]:
        """Return the active connections granted to the organization of the context.

        Autorização: infraestrutura em ``system_context`` da organização liberada (jobs).
        Erros: Unavailable("remote_call_in_request") fora de um job.
        Efeitos: nenhum.
        """
        from skeleton.integrations.remote_instances.models import RemoteInstanceConnection
        from skeleton.tenancy.context import current_context

        context = current_context()
        if not context.system:
            raise Unavailable("remote_call_in_request")
        return tuple(
            RemoteConnectionInfo(
                row.pk, row.label, row.base_url, row.expected_instance_id, row.last_checked_at, row.last_error_code
            )
            for row in RemoteInstanceConnection.objects.filter(
                is_active=True, granted_organization_id=context.organization_id
            ).order_by("label", "sysid")
        )

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def instance(self, *, actor: AbstractBaseUser | None, connection_id: UUID) -> RemoteInstanceInfo:
        """Return the identity and the apps of the remote instance.

        Autorização: infraestrutura em ``system_context`` da organização liberada (jobs).
        Erros: NotFound (conexão de outra organização ou inativa); Unavailable("remote_call_in_request");
        Conflict com o código do erro remoto (``remote_unauthorized``, ``remote_instance_mismatch``...).
        Efeitos: uma chamada de rede à instância remota.
        """
        data = _get(_connection(connection_id), "instance/")
        return RemoteInstanceInfo(
            instance_id=UUID(str(data["instance_id"])),
            name=str(data.get("name", "")),
            purpose=str(data.get("purpose", "")),
            product_name=str(data.get("product_name", "")),
            core_version=data.get("core_version"),
            billing_enabled=bool(data.get("billing_enabled")),
            apps=tuple(str(app.get("app_label")) for app in data.get("apps") or ()),
        )

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def organizations(
        self,
        *,
        actor: AbstractBaseUser | None,
        connection_id: UUID,
        updated_since: datetime | None = None,
        cursor: str | None = None,
    ) -> RemoteOrganizationPage:
        """Return one page of the remote organizations, by the remote's cursor.

        Autorização: infraestrutura em ``system_context`` da organização liberada (jobs).
        Erros: os de ``instance``.
        Efeitos: uma chamada de rede à instância remota.
        """
        params = {"updated_since": updated_since.isoformat() if updated_since else None, "cursor": cursor}
        data = _get(_connection(connection_id), "organizations/", params)
        try:
            items = tuple(_organization(item) for item in data.get("results") or ())
        except (KeyError, ValueError, TypeError):
            raise Conflict("remote_invalid_response") from None
        return RemoteOrganizationPage(items=items, next_cursor=data.get("next_cursor"))

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def organization(
        self, *, actor: AbstractBaseUser | None, connection_id: UUID, organization_id: UUID
    ) -> RemoteOrganization:
        """Return one remote organization.

        Autorização: infraestrutura em ``system_context`` da organização liberada (jobs).
        Erros: os de ``instance``; NotFound para organização remota inexistente.
        Efeitos: uma chamada de rede à instância remota.
        """
        data = _get(_connection(connection_id), f"organizations/{UUID(str(organization_id))}/")
        try:
            return _organization(data)
        except (KeyError, ValueError, TypeError):
            raise Conflict("remote_invalid_response") from None

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def invoices(
        self, *, actor: AbstractBaseUser | None, connection_id: UUID, organization_id: UUID, page: int = 1
    ) -> tuple[RemoteInvoice, ...]:
        """Return one page of the invoices of a remote organization.

        Autorização: infraestrutura em ``system_context`` da organização liberada (jobs).
        Erros: os de ``instance``.
        Efeitos: uma chamada de rede à instância remota.
        """
        path = f"organizations/{UUID(str(organization_id))}/invoices/"
        data = _get(_connection(connection_id), path, {"page": page})
        try:
            return tuple(
                RemoteInvoice(
                    id=UUID(str(item["id"])),
                    number=item.get("number"),
                    status=str(item["status"]),
                    currency=str(item["currency"]),
                    total=int(item["total"]),
                    paid=int(item.get("paid") or 0),
                    issued_at=_moment(item.get("issued_at")),
                    due_at=_moment(item.get("due_at")),
                    paid_at=_moment(item.get("paid_at")),
                )
                for item in data.get("results") or ()
            )
        except (KeyError, ValueError, TypeError):
            raise Conflict("remote_invalid_response") from None

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def payments(
        self, *, actor: AbstractBaseUser | None, connection_id: UUID, organization_id: UUID, page: int = 1
    ) -> tuple[RemotePayment, ...]:
        """Return one page of the payments of a remote organization.

        Autorização: infraestrutura em ``system_context`` da organização liberada (jobs).
        Erros: os de ``instance``.
        Efeitos: uma chamada de rede à instância remota.
        """
        path = f"organizations/{UUID(str(organization_id))}/payments/"
        data = _get(_connection(connection_id), path, {"page": page})
        try:
            return tuple(
                RemotePayment(
                    id=UUID(str(item["id"])),
                    invoice_id=UUID(str(item["invoice_id"])) if item.get("invoice_id") else None,
                    method=str(item["method"]),
                    status=str(item["status"]),
                    amount=int(item["amount"]),
                    currency=str(item["currency"]),
                    created_at=_required_moment(item.get("created_at")),
                    confirmed_at=_moment(item.get("confirmed_at")),
                )
                for item in data.get("results") or ()
            )
        except (KeyError, ValueError, TypeError):
            raise Conflict("remote_invalid_response") from None

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def metrics(
        self,
        *,
        actor: AbstractBaseUser | None,
        connection_id: UUID,
        since: date,
        until: date,
        codes: tuple[str, ...] = (),
    ) -> tuple[RemoteMetric, ...]:
        """Return the daily metrics of the remote instance in ``[since, until]`` (at most 400 days).

        Autorização: infraestrutura em ``system_context`` da organização liberada (jobs).
        Erros: os de ``instance``.
        Efeitos: uma chamada de rede à instância remota.
        """
        params: dict[str, Any] = {"since": since.isoformat(), "until": until.isoformat()}
        if codes:
            params["code"] = list(codes)
        data = _get(_connection(connection_id), "metrics/", params)
        try:
            return tuple(
                RemoteMetric(
                    day=parse_date(str(item["day"])) or date.min,
                    code=str(item["code"]),
                    currency=str(item.get("currency", "")),
                    value=int(item["value"]),
                )
                for item in data.get("results") or ()
            )
        except (KeyError, ValueError, TypeError):
            raise Conflict("remote_invalid_response") from None
