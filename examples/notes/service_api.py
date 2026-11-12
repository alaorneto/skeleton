"""Service API of the Notes example: the only module other apps may import (docs/service-api.md).

Views and the REST summary of this app call it too, so there is a single authorized path. The
note body is a sensitive field (TableSpec.sensitive_fields) and never leaves through a DTO.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from django.utils.translation import gettext

from skeleton.access.policies import authorized_queryset, require_record_action
from skeleton.service_api import InvalidInput, NotFound, Page, ServiceAPI, as_utc, as_uuid, paginate, service_method
from skeleton.tenancy.context import current_context

from .flows import PUBLICATION
from .models import Note

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser as User

APP_LABEL = "example_notes"
_TEXT_FIELDS = ("code", "title", "body")


@dataclass(frozen=True, slots=True)
class NoteSummary:
    """A note as other apps see it: identification and revision, never the body."""

    sysid: UUID
    code: str
    title: str
    visibility: str
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PublicationStatus:
    """The publication request of a note: the flow instance, its state and its result."""

    instance_id: UUID
    number: str
    status: str
    outcome: str
    revision: int
    created: bool


def _summary(note: Note) -> NoteSummary:
    return NoteSummary(
        sysid=note.pk,
        code=note.code,
        title=note.title,
        visibility=note.visibility,
        revision=note.revision,
        created_at=as_utc(note.created_at),
        updated_at=as_utc(note.updated_at),
    )


def _text(values: dict[str, object]) -> dict[str, str]:
    errors = {name: [gettext("Use um texto.")] for name, value in values.items() if not isinstance(value, str)}
    if errors:
        raise InvalidInput(errors)
    return {name: str(value) for name, value in values.items()}


def _readable(actor: "User", note_id: object) -> Note:
    note = authorized_queryset(Note, user=actor, action="read").filter(pk=as_uuid(note_id, field="note_id")).first()
    if note is None:
        raise NotFound()
    return note


class NoteService(ServiceAPI):
    """Notes of the current organization, as the actor may read and change them."""

    app_label = APP_LABEL

    @service_method(read_only=True, since="0.3.0")
    def note_count(self, *, actor: "User") -> int:
        """Counts the notes the actor can read in the current organization.

        Autorização: TablePermission read em example_notes.note; conta só registros legíveis.
        Erros: Forbidden sem a permissão de tabela; Unavailable com a app desativada.
        Efeitos: nenhum.
        """
        return authorized_queryset(Note, user=actor, action="read").count()

    @service_method(read_only=True, since="0.3.0")
    def list_notes(
        self, *, actor: "User", code: str | None = None, cursor: str | None = None, page_size: int = 25
    ) -> Page[NoteSummary]:
        """Lists readable notes ordered by creation, optionally by exact code.

        Autorização: TablePermission read em example_notes.note; filtra antes de paginar.
        Erros: InvalidInput para cursor, page_size (1 a 100) ou code inválidos; Forbidden sem a
        permissão de tabela; Unavailable com a app desativada.
        Efeitos: nenhum.
        """
        rows = authorized_queryset(Note, user=actor, action="read").order_by("created_at", "sysid")
        if code is not None:
            rows = rows.filter(code=_text({"code": code})["code"])
        return paginate(rows, convert=_summary, cursor=cursor, page_size=page_size)

    @service_method(read_only=True, since="0.3.0")
    def get_note(self, *, actor: "User", note_id: UUID) -> NoteSummary:
        """Returns one note readable by the actor.

        Autorização: TablePermission read em example_notes.note e acesso de leitura ao registro.
        Erros: NotFound se a nota não existir, for de outra organização ou não for legível;
        Forbidden sem a permissão de tabela; Unavailable com a app desativada.
        Efeitos: nenhum.
        """
        return _summary(_readable(actor, note_id))

    @service_method(read_only=False, since="0.3.0")
    def create_note(self, *, actor: "User", code: str, title: str, body: str = "") -> NoteSummary:
        """Creates a private note owned by the actor.

        Autorização: TablePermission create em example_notes.note.
        Erros: InvalidInput para campos vazios, longos ou de tipo errado; Conflict(integrity_conflict)
        se outro registro vivo usar o mesmo code; Forbidden sem a permissão; Unavailable com a app
        desativada. O code único torna a repetição segura: não há idempotency_key.
        Efeitos: grava Note (auditada por trigger na transação do chamador).
        """
        values = _text({"code": code, "title": title, "body": body})
        authorized_queryset(Note, user=actor, action="create")
        note = Note(organization_id=current_context().organization_id, owner=actor, visibility="private", **values)
        note.save()
        return _summary(note)

    @service_method(read_only=False, since="0.3.0")
    def update_note(
        self,
        *,
        actor: "User",
        note_id: UUID,
        expected_revision: int,
        code: str | None = None,
        title: str | None = None,
        body: str | None = None,
    ) -> NoteSummary:
        """Changes code, title or body of a note at the expected revision.

        Autorização: TablePermission update em example_notes.note e acesso de escrita ao registro.
        Erros: NotFound se não for legível; Forbidden sem escrita; InvalidInput sem campos ou com
        valores inválidos; Conflict(stale_revision) se a revisão mudou; Conflict(integrity_conflict)
        para code repetido; Unavailable com a app desativada.
        Efeitos: atualiza Note (auditada); nunca tenta de novo em silêncio.
        """
        changes = {
            name: value for name, value in zip(_TEXT_FIELDS, (code, title, body), strict=True) if value is not None
        }
        if not changes:
            raise InvalidInput({"non_field_errors": [gettext("Informe ao menos um campo.")]})
        if type(expected_revision) is not int:
            raise InvalidInput({"expected_revision": [gettext("Use a revisão inteira atual.")]})
        values = _text(changes)
        note = require_record_action(_readable(actor, note_id), user=actor, action="update")
        note.revision = expected_revision  # CAS: save() raises ConcurrentUpdate -> Conflict(stale_revision).
        for name, value in values.items():
            setattr(note, name, value)
        note.save(update_fields=set(values))
        return _summary(note)

    @service_method(read_only=False, since="0.4.0")
    def request_publication(
        self, *, actor: "User", note_id: UUID, idempotency_key: str | None = None
    ) -> PublicationStatus:
        """Starts the publication flow of a note (docs/flows.md).

        Autorização: leitura da nota e escrita nela (a ação de início do fluxo é update).
        Erros: NotFound se a nota não existir, for de outra organização ou não for legível;
        Forbidden sem escrita na nota; Conflict(already_running) se a nota já tem um pedido em
        andamento; Unavailable com a app desativada.
        Efeitos: inicia a instância do fluxo example_notes.publication e abre a tarefa de
        preparação para o ator, na transação do chamador. A chave de idempotência repetida
        devolve o mesmo pedido (created=False).
        """
        from skeleton.service_api.flows import FlowService

        note = _readable(actor, note_id)
        receipt = FlowService().start(
            actor=actor, flow_key=PUBLICATION, subject_id=note.pk, idempotency_key=idempotency_key
        )
        return PublicationStatus(receipt.sysid, receipt.number, receipt.status, "", receipt.revision, receipt.created)

    @service_method(read_only=True, since="0.4.0")
    def publication_status(self, *, actor: "User", note_id: UUID) -> PublicationStatus | None:
        """Returns the latest publication request of a note, or None when there is none.

        Autorização: leitura da nota; o pedido é o que o ator pode ler pelo FlowService.
        Erros: NotFound se a nota não existir, for de outra organização ou não for legível;
        Unavailable com a app desativada.
        Efeitos: nenhum.
        """
        from skeleton.service_api.flows import FlowService

        note = _readable(actor, note_id)
        page = FlowService().instances(actor=actor, flow_key=PUBLICATION, subject_id=note.pk, page_size=1)
        if not page.items:
            return None
        latest = page.items[0]
        return PublicationStatus(latest.sysid, latest.number, latest.status, latest.outcome, latest.revision, False)
