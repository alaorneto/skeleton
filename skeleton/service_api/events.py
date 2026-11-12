"""EventService: publish domain events of the calling app in the write's transaction (#108)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext

from .applications import calling_app_label
from .base import ServiceAPI, service_method
from .dto import as_utc, as_uuid
from .errors import Forbidden, InvalidInput, NotFound
from .identity import _user

Scalar = str | int | bool | None


@dataclass(frozen=True, slots=True)
class EventReceipt:
    sysid: UUID
    event_type: str
    record_id: UUID
    occurred_at: datetime


class EventService(ServiceAPI):
    """Event types are registered in ``ready()``; only the owning app publishes them."""

    @service_method(read_only=False, since="0.5.0", allow_system=True)
    def publish(
        self,
        *,
        actor: AbstractBaseUser | None,
        event_type: str,
        record_id: UUID,
        data: tuple[tuple[str, Scalar], ...] = (),
    ) -> EventReceipt:
        """Publish an event about a record of the current organization.

        Autorização: o tipo pertence à app chamadora; o ator lê o registro (em ``system_context``
        basta o registro existir na organização).
        Erros: Forbidden para tipo de outra app; NotFound para registro inexistente, de outra
        organização ou não legível; InvalidInput para tipo desconhecido ou campo não declarado.
        Efeitos: grava o evento e um job por consumidor; o rollback do chamador desfaz tudo.
        """
        from skeleton.access.policies import authorized_queryset
        from skeleton.events.registry import get_event_type
        from skeleton.events.services import publish_event
        from skeleton.system.registry import get_spec

        try:
            kind = get_event_type(event_type)
        except KeyError:
            raise InvalidInput({"event_type": [gettext("Tipo de evento desconhecido.")]}) from None
        if calling_app_label() != (kind.owner_app or None):
            raise Forbidden(gettext("A app só publica os próprios tipos de evento."))
        identifier = as_uuid(record_id, field="record_id")
        model = get_spec(kind.table_key).model
        try:
            if actor is None:
                record = model.objects.filter(pk=identifier).first()
            else:
                record = authorized_queryset(model, user=_user(actor), action="read").filter(pk=identifier).first()
        except PermissionDenied:
            record = None
        if record is None:
            raise NotFound()
        event = publish_event(kind.code, record=record, data=dict(data))
        return EventReceipt(event.pk, event.event_type, event.record_sysid, as_utc(event.occurred_at))
