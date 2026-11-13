"""FlowService: the door of apps and surfaces to the business process flows (docs/flows.md).

Flows are declared by each app with ``skeleton.flows.registry``; this service starts and
operates their instances. It is a core service (no ``app_label``): every method requires
the app that owns the flow to be enabled in the organization and answers ``Unavailable``
otherwise. Authorization is revalidated inside ``skeleton.flows`` on every call.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.utils.translation import gettext

from .applications import calling_app_label
from .base import ServiceAPI, service_method
from .dto import DEFAULT_PAGE_SIZE, Page, as_utc, as_uuid, paginate
from .errors import Conflict, Forbidden, InvalidInput, NotFound, Unavailable

__all__ = [
    "ActivityEntry",
    "FlowService",
    "FlowValue",
    "FormFieldInfo",
    "InstanceInfo",
    "SignalReceipt",
    "StartReceipt",
    "StepInfo",
    "TaskAction",
    "TaskDetail",
    "TaskSummary",
]

Scalar = str | int | bool | None
FormInput = str | int | bool | Decimal | date | datetime | UUID | None
FormData = tuple[tuple[str, FormInput], ...]
TASK_SCOPES = ("mine", "available", "all")
TASK_STATES = ("open", "completed", "cancelled")
INSTANCE_STATES = ("running", "suspended", "completed", "cancelled", "failed")


@dataclass(frozen=True, slots=True)
class FlowValue:
    name: str
    value: Scalar


@dataclass(frozen=True, slots=True)
class StepInfo:
    key: str
    label: str
    kind: str
    status: str


@dataclass(frozen=True, slots=True)
class InstanceInfo:
    """An instance as its readers see it: state, subject and the non-sensitive form data."""

    sysid: UUID
    flow_key: str
    flow_version: int
    label: str
    number: str
    status: str
    outcome: str
    subject_table: str
    subject_id: UUID
    starter_id: UUID | None
    revision: int
    created_at: datetime
    finished_at: datetime | None
    data: tuple[FlowValue, ...]
    active_steps: tuple[StepInfo, ...]


@dataclass(frozen=True, slots=True)
class StartReceipt:
    sysid: UUID
    number: str
    status: str
    revision: int
    created: bool


@dataclass(frozen=True, slots=True)
class SignalReceipt:
    delivered: bool


@dataclass(frozen=True, slots=True)
class TaskSummary:
    sysid: UUID
    instance_id: UUID
    flow_key: str
    flow_label: str
    number: str
    step_key: str
    step_label: str
    kind: str
    status: str
    assignee_id: UUID | None
    queue: str
    decision: str
    due_at: datetime | None
    revision: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FormFieldInfo:
    name: str
    type: str
    label: str
    required: bool
    sensitive: bool
    choices: tuple[str, ...]
    help_text: str
    max_length: int
    value: Scalar


@dataclass(frozen=True, slots=True)
class TaskAction:
    """One way to complete a task: the key to send and the extra fields of that transition."""

    key: str
    label: str
    fields: tuple[FormFieldInfo, ...]


@dataclass(frozen=True, slots=True)
class TaskDetail:
    task: TaskSummary
    subject_table: str
    subject_id: UUID
    instance_status: str
    fields: tuple[FormFieldInfo, ...]
    actions: tuple[TaskAction, ...]
    data: tuple[FlowValue, ...]
    can_act: bool
    can_claim: bool
    can_release: bool
    can_reassign: bool


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    sequence: int
    kind: str
    step_key: str
    step_label: str
    actor_id: UUID | None
    occurred_at: datetime
    detail: tuple[FlowValue, ...]
    comment: str


@contextmanager
def _flow_errors() -> Iterator[None]:
    """Translate the engine's own errors; the rest goes through ``service_method``."""
    from skeleton.flows.errors import FlowConflict, FlowUnavailable

    try:
        yield
    except FlowUnavailable:
        raise Unavailable() from None
    except FlowConflict as conflict:
        raise Conflict(conflict.code, conflict.detail) from None


def _values(mapping: Any) -> tuple[FlowValue, ...]:
    result = []
    for name, value in sorted(dict(mapping).items()):
        if value is not None and not isinstance(value, str | int | bool):
            value = str(value)
        result.append(FlowValue(str(name), value))
    return tuple(result)


def _data(data: FormData) -> dict[str, Any]:
    if not isinstance(data, tuple) or any(not isinstance(item, tuple) or len(item) != 2 for item in data):
        raise InvalidInput({"data": ["Envie pares (campo, valor)."]})
    names = [item[0] for item in data]
    if len(set(names)) != len(names):
        raise InvalidInput({"data": [gettext("Campo repetido.")]})
    return dict(data)


def _optional_utc(value: datetime | None) -> datetime | None:
    return as_utc(value) if value is not None else None


def _spec(row: Any) -> Any:
    from skeleton.flows.engine import specification_of

    return specification_of(row.definition)


def _step_label(spec: Any, key: str) -> str:
    return str(spec.step(key).title) if key and spec.has_step(key) else key


def _instance(row: Any) -> InstanceInfo:
    spec = _spec(row)
    runs = row.runs.filter(status__in=("active", "waiting", "failed")).order_by("sequence")
    return InstanceInfo(
        sysid=row.pk,
        flow_key=row.flow_key,
        flow_version=row.flow_version,
        label=spec.label,
        number=row.number,
        status=row.status,
        outcome=row.outcome,
        subject_table=row.subject_table,
        subject_id=row.subject_sysid,
        starter_id=row.starter_id,
        revision=row.revision,
        created_at=as_utc(row.created_at),
        finished_at=_optional_utc(row.finished_at),
        data=_values(row.data),
        active_steps=tuple(
            StepInfo(run.step_key, _step_label(spec, run.step_key), run.kind, run.status) for run in runs
        ),
    )


def _task(row: Any) -> TaskSummary:
    spec = _spec(row.instance)
    queue = "group" if row.candidate_group_id else "role" if row.candidate_role_id else ""
    return TaskSummary(
        sysid=row.pk,
        instance_id=row.instance_id,
        flow_key=row.instance.flow_key,
        flow_label=spec.label,
        number=row.instance.number,
        step_key=row.step_key,
        step_label=_step_label(spec, row.step_key),
        kind=row.kind,
        status=row.status,
        assignee_id=row.assignee_id,
        queue=queue,
        decision=row.decision,
        due_at=_optional_utc(row.due_at),
        revision=row.revision,
        created_at=as_utc(row.created_at),
    )


def _fields(form: Any, values: dict[str, Any], *, reveal: bool) -> tuple[FormFieldInfo, ...]:
    if form is None:
        return ()
    return tuple(
        FormFieldInfo(
            name=item.name,
            type=item.type,
            label=item.label or item.name,
            required=item.required,
            sensitive=item.sensitive,
            choices=tuple(item.choices),
            help_text=item.help_text,
            max_length=item.max_length,
            value=values.get(item.name) if reveal or not item.sensitive else None,
        )
        for item in form.fields
    )


def _readable_instance(actor: Any, instance_id: Any) -> Any:
    from skeleton.flows import policies
    from skeleton.flows.models import FlowInstance

    identifier = as_uuid(instance_id, field="instance_id")
    row = FlowInstance.objects.select_related("definition").filter(pk=identifier).first()
    if row is None:
        raise NotFound()
    policies.require_flow_app(row.definition.app_label)
    if not policies.visible_instances(actor).filter(pk=row.pk).exists():
        raise NotFound()
    return row


def _readable_task(actor: Any, task_id: Any) -> Any:
    from skeleton.flows import policies
    from skeleton.flows.models import FlowTask

    identifier = as_uuid(task_id, field="task_id")
    row = FlowTask.objects.select_related("instance__definition").filter(pk=identifier).first()
    if row is None:
        raise NotFound()
    policies.require_flow_app(row.instance.definition.app_label)
    if not policies.visible_tasks(actor).filter(pk=row.pk).exists():
        raise NotFound()
    return row


def _owned_flow(flow_key: str) -> None:
    from django.core.exceptions import ValidationError

    from skeleton.flows.services import published_spec

    try:
        spec = published_spec(flow_key)  # Declared in code or published as data (#414).
    except ValidationError:
        raise InvalidInput({"flow_key": [gettext("Fluxo não declarado.")]}) from None
    if calling_app_label() != spec.owner_app:
        raise Forbidden(gettext("A app só inicia e sinaliza os próprios fluxos."))


class FlowService(ServiceAPI):
    """Start, read and operate instances and tasks of the flows declared by the apps."""

    @service_method(read_only=False, since="0.6.0", allow_system=True)
    def start(
        self,
        *,
        actor: AbstractBaseUser | None,
        flow_key: str,
        subject_id: UUID,
        data: FormData = (),
        idempotency_key: str | None = None,
    ) -> StartReceipt:
        """Start the published version of a flow of the calling app about one of its records.

        Autorização: a app chamadora é a dona do fluxo; o ator lê o registro de negócio e tem sobre
        ele a ação de início do fluxo (``update`` por padrão). Em ``system_context``, ``actor=None``.
        Erros: Forbidden para fluxo de outra app ou sem a ação de início; NotFound para registro
        inexistente, de outra organização ou não legível; InvalidInput para fluxo não declarado ou
        dados fora do formulário de início; Conflict(already_running) se o registro já tem uma
        instância ativa do fluxo; Conflict(definition_changed) se o fluxo mudou sem versão nova;
        Unavailable com a app dona desativada.
        Efeitos: grava a definição congelada (no primeiro uso), a instância, o histórico, eventos
        e os jobs e tarefas das primeiras etapas, tudo na transação do chamador. A chave de
        idempotência repetida devolve a mesma instância (``created=False``).
        """
        from skeleton.flows import services

        _owned_flow(flow_key)
        with _flow_errors():
            instance, created = services.start_instance(
                flow_key,
                subject_id=as_uuid(subject_id, field="subject_id"),
                actor=actor,
                data=_data(data),
                idempotency_key=idempotency_key,
            )
        return StartReceipt(instance.pk, instance.number, instance.status, instance.revision, created)

    @service_method(read_only=False, since="0.6.0", allow_system=True)
    def signal(
        self, *, actor: AbstractBaseUser | None, instance_id: UUID, name: str, data: FormData = ()
    ) -> SignalReceipt:
        """Deliver a named signal to the waits of an instance of the calling app's flow.

        Autorização: a app chamadora é a dona do fluxo; o ator lê a instância. Em
        ``system_context``, ``actor=None`` (consumidores de eventos e jobs da app).
        Erros: Forbidden para fluxo de outra app; NotFound para instância inexistente, de outra
        organização ou não legível; InvalidInput para dados fora do formulário da espera;
        Conflict(instance_suspended); Unavailable com a app dona desativada.
        Efeitos: conclui as esperas daquele sinal e avança a instância. Sem espera pelo sinal,
        nada muda e ``delivered`` é falso: repetir a entrega é inofensivo.
        """
        from skeleton.flows import services
        from skeleton.flows.models import FlowInstance

        identifier = as_uuid(instance_id, field="instance_id")
        flow_key = FlowInstance.objects.filter(pk=identifier).values_list("flow_key", flat=True).first()
        if flow_key is None:
            raise NotFound()
        _owned_flow(flow_key)
        with _flow_errors():
            return SignalReceipt(services.send_signal(identifier, name, actor=actor, data=_data(data)))

    @service_method(read_only=True, since="0.6.0")
    def instance(self, *, actor: AbstractBaseUser, instance_id: UUID) -> InstanceInfo:
        """One instance the actor may read.

        Autorização: quem lê o registro de negócio, quem iniciou, quem tem ou teve tarefa na
        instância, quem é candidato a uma tarefa aberta dela, ou o administrador da organização.
        Erros: NotFound se não existir, for de outra organização ou não for legível; Unavailable
        com a app dona desativada.
        Efeitos: nenhum.
        """
        with _flow_errors():
            return _instance(_readable_instance(actor, instance_id))

    @service_method(read_only=True, since="0.6.0")
    def instances(
        self,
        *,
        actor: AbstractBaseUser,
        flow_key: str | None = None,
        subject_id: UUID | None = None,
        status: str | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[InstanceInfo]:
        """Instances the actor may read, newest first, optionally of one flow, subject or state.

        Autorização: a mesma de ``instance``, aplicada antes de paginar; instâncias de apps
        desativadas são omitidas.
        Erros: InvalidInput para filtro, cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.flows import policies

        rows = policies.visible_instances(actor).select_related("definition")
        if flow_key is not None:
            if not isinstance(flow_key, str):
                raise InvalidInput({"flow_key": [gettext("Use a chave do fluxo.")]})
            rows = rows.filter(flow_key=flow_key)
        if subject_id is not None:
            rows = rows.filter(subject_sysid=as_uuid(subject_id, field="subject_id"))
        if status is not None:
            if status not in INSTANCE_STATES:
                raise InvalidInput({"status": [gettext("Estado inválido.")]})
            rows = rows.filter(status=status)
        return paginate(rows.order_by("-created_at", "sysid"), convert=_instance, cursor=cursor, page_size=page_size)

    @service_method(read_only=True, since="0.6.0")
    def history(
        self,
        *,
        actor: AbstractBaseUser,
        instance_id: UUID,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[ActivityEntry]:
        """The ordered history of an instance: who did what and when.

        Autorização: a mesma de ``instance``. O comentário de cada passo é devolvido a quem lê a
        instância (é texto do processo; só a auditoria o redige); valores sensíveis de formulário
        nunca aparecem aqui.
        Erros: NotFound; InvalidInput para cursor ou tamanho de página; Unavailable.
        Efeitos: nenhum.
        """
        from skeleton.flows.models import FlowActivity

        with _flow_errors():
            row = _readable_instance(actor, instance_id)
        spec = _spec(row)

        def convert(item: Any) -> ActivityEntry:
            return ActivityEntry(
                sequence=item.sequence,
                kind=item.kind,
                step_key=item.step_key,
                step_label=_step_label(spec, item.step_key),
                actor_id=item.created_by,
                occurred_at=as_utc(item.created_at),
                detail=_values(
                    {key: value for key, value in item.detail.items() if not isinstance(value, dict | list)}
                ),
                comment=item.comment,
            )

        rows = FlowActivity.objects.filter(instance=row).order_by("sequence")
        return paginate(rows, convert=convert, cursor=cursor, page_size=page_size)

    @service_method(read_only=True, since="0.6.0")
    def tasks(
        self,
        *,
        actor: AbstractBaseUser,
        scope: str = "mine",
        status: str | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[TaskSummary]:
        """The actor's task inbox: ``mine`` (assigned and open), ``available`` (queues) or ``all``.

        Autorização: ``mine`` e ``available`` trazem só tarefas abertas em que o ator pode agir
        agora; ``all`` traz as que ele pode ler (as dele, passadas e presentes; todas para o
        administrador da organização). Tarefas de apps desativadas são omitidas.
        Erros: InvalidInput para escopo, estado, cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.flows import policies

        if scope not in TASK_SCOPES:
            raise InvalidInput({"scope": [gettext("Use mine, available ou all.")]})
        if status is not None and (status not in TASK_STATES or scope != "all"):
            raise InvalidInput({"status": [gettext("Estado inválido para este escopo.")]})
        if scope == "all":
            rows = policies.visible_tasks(actor)
            if status is not None:
                rows = rows.filter(status=status)
        else:
            rows = policies.actionable_tasks(actor).filter(assignee__isnull=scope == "available")
        rows = rows.select_related("instance__definition").order_by("-created_at", "sysid")
        return paginate(rows, convert=_task, cursor=cursor, page_size=page_size)

    @service_method(read_only=True, since="0.6.0")
    def open_task_count(self, *, actor: AbstractBaseUser) -> int:
        """How many open tasks are assigned to the actor in the current organization.

        Autorização: só as tarefas do próprio ator, de apps ativas.
        Erros: nenhum além do contrato do contexto.
        Efeitos: nenhum.
        """
        from skeleton.flows import policies

        return int(policies.actionable_tasks(actor).filter(assignee_id=actor.pk).count())

    @service_method(read_only=True, since="0.6.0")
    def task(self, *, actor: AbstractBaseUser, task_id: UUID) -> TaskDetail:
        """One task with what is needed to act on it: form fields, actions and the instance data.

        Autorização: o responsável, os candidatos da fila, quem já participou da tarefa e o
        administrador da organização. Os valores atuais de campos sensíveis do formulário só são
        devolvidos a quem pode agir na tarefa agora (``can_act``); para os demais vêm vazios.
        Erros: NotFound se não existir, for de outra organização ou não for legível; Unavailable.
        Efeitos: nenhum.
        """
        from skeleton.flows import policies
        from skeleton.flows.engine import Execution
        from skeleton.flows.tasks import available_actions

        with _flow_errors():
            row = _readable_task(actor, task_id)
        execution = Execution(row.instance)
        step = execution.spec.step(row.step_key)
        live = row.status == "open" and row.instance.status == "running"
        can_act = live and policies.actionable_tasks(actor).filter(pk=row.pk).exists()
        values = {**row.instance.data, **(row.instance.sensitive_data if can_act else {})}
        actions: tuple[TaskAction, ...] = ()
        if row.kind == "task" and live:
            actions = tuple(
                TaskAction(item.ident, item.label or item.ident, _fields(item.form, values, reveal=can_act))
                for item in available_actions(execution, step)
            )
        mine = row.assignee_id is not None and row.assignee_id == actor.pk
        return TaskDetail(
            task=_task(row),
            subject_table=row.instance.subject_table,
            subject_id=row.instance.subject_sysid,
            instance_status=row.instance.status,
            fields=_fields(step.form, values, reveal=can_act),
            actions=actions,
            data=_values(row.instance.data),
            can_act=can_act,
            can_claim=can_act and row.assignee_id is None,
            can_release=can_act and mine and bool(row.candidate_group_id or row.candidate_role_id),
            can_reassign=live and (policies.is_flow_admin(actor) or (mine and step.delegable)),
        )

    @service_method(read_only=False, since="0.6.0")
    def claim_task(self, *, actor: AbstractBaseUser, task_id: UUID, expected_revision: int) -> TaskSummary:
        """Take a queue task for the actor.

        Autorização: candidato da fila (grupo ou papel) no momento da chamada.
        Erros: NotFound; Forbidden; Conflict(stale_revision); Conflict(not_a_queue_task);
        Conflict(instance_suspended|instance_closed|task_closed); Unavailable.
        Efeitos: define o responsável e registra no histórico.
        """
        from skeleton.flows import tasks

        with _flow_errors():
            return _task(tasks.claim_task(task_id, actor=actor, expected_revision=expected_revision))

    @service_method(read_only=False, since="0.6.0")
    def release_task(self, *, actor: AbstractBaseUser, task_id: UUID, expected_revision: int) -> TaskSummary:
        """Give a claimed queue task back to its queue.

        Autorização: o responsável atual de uma tarefa de fila.
        Erros: NotFound; Forbidden; Conflict(stale_revision); Conflict(not_claimed); Unavailable.
        Efeitos: remove o responsável e registra no histórico.
        """
        from skeleton.flows import tasks

        with _flow_errors():
            return _task(tasks.release_task(task_id, actor=actor, expected_revision=expected_revision))

    @service_method(read_only=False, since="0.6.0")
    def reassign_task(
        self, *, actor: AbstractBaseUser, task_id: UUID, expected_revision: int, to_user_id: UUID, comment: str = ""
    ) -> TaskSummary:
        """Hand a task to another person with an active membership.

        Autorização: o responsável, quando a etapa permite delegação, ou o administrador da
        organização.
        Erros: NotFound; Forbidden; InvalidInput (pessoa sem vínculo ativo, a mesma pessoa);
        Conflict(stale_revision); Conflict de estado; Unavailable.
        Efeitos: troca o responsável, guarda quem delegou, registra no histórico e notifica.
        """
        from skeleton.flows import tasks

        with _flow_errors():
            row = tasks.reassign_task(
                task_id,
                actor=actor,
                expected_revision=expected_revision,
                to_user_id=as_uuid(to_user_id, field="to_user_id"),
                comment=comment,
            )
        return _task(row)

    @service_method(read_only=False, since="0.6.0")
    def complete_task(
        self,
        *,
        actor: AbstractBaseUser,
        task_id: UUID,
        expected_revision: int,
        action: str | None = None,
        data: FormData = (),
        comment: str = "",
    ) -> TaskSummary:
        """Complete a task by one of its actions, with the declared form data.

        Autorização: o responsável, ou um candidato da fila (que passa a ser o responsável).
        Erros: NotFound; Forbidden; InvalidInput (ação indisponível, campo não declarado,
        obrigatório ausente, valor inválido); Conflict(stale_revision); Conflict(not_a_task);
        Conflict(flow_failed) se a definição não puder avançar; Conflict de estado; Unavailable.
        Efeitos: grava o envio do formulário, conclui a tarefa e avança a instância (novas
        tarefas, jobs, notificações e eventos) na transação do chamador.
        """
        from skeleton.flows import tasks

        with _flow_errors():
            row = tasks.complete_task(
                task_id,
                actor=actor,
                expected_revision=expected_revision,
                action=action,
                data=_data(data),
                comment=comment,
            )
        return _task(row)

    @service_method(read_only=False, since="0.6.0")
    def decide_task(
        self,
        *,
        actor: AbstractBaseUser,
        task_id: UUID,
        expected_revision: int,
        decision: str,
        data: FormData = (),
        comment: str = "",
    ) -> TaskSummary:
        """Approve (``approved``) or reject (``rejected``) as one approver of an approval step.

        Autorização: o responsável, ou um candidato da fila.
        Erros: NotFound; Forbidden; InvalidInput; Conflict(stale_revision);
        Conflict(not_an_approval); Conflict(flow_failed); Conflict de estado; Unavailable.
        Efeitos: grava a decisão e, quando a política da etapa se resolve, cancela as demais
        tarefas e avança a instância, na transação do chamador.
        """
        from skeleton.flows import tasks

        with _flow_errors():
            row = tasks.decide_task(
                task_id,
                actor=actor,
                expected_revision=expected_revision,
                decision=decision,
                data=_data(data),
                comment=comment,
            )
        return _task(row)

    @service_method(read_only=False, since="0.6.0")
    def cancel(
        self, *, actor: AbstractBaseUser, instance_id: UUID, expected_revision: int, reason: str = ""
    ) -> InstanceInfo:
        """Cancel an instance and its open tasks.

        Autorização: o administrador da organização, ou quem iniciou quando o fluxo permite.
        Erros: NotFound; Forbidden; Conflict(stale_revision); Conflict(instance_closed); Unavailable.
        Efeitos: encerra etapas e tarefas abertas, registra o motivo e publica o evento.
        """
        from skeleton.flows import services

        with _flow_errors():
            row = services.cancel_instance(
                as_uuid(instance_id, field="instance_id"),
                actor=actor,
                expected_revision=expected_revision,
                reason=reason,
            )
        return _instance(row)

    @service_method(read_only=False, since="0.6.0")
    def suspend(
        self, *, actor: AbstractBaseUser, instance_id: UUID, expected_revision: int, reason: str = ""
    ) -> InstanceInfo:
        """Freeze a running instance: task actions are refused and jobs do nothing until ``resume``.

        Autorização: o administrador da organização.
        Erros: NotFound; Forbidden; Conflict(stale_revision); Conflict(not_running); Unavailable.
        Efeitos: muda o estado e registra o motivo.
        """
        from skeleton.flows import services

        with _flow_errors():
            row = services.suspend_instance(
                as_uuid(instance_id, field="instance_id"),
                actor=actor,
                expected_revision=expected_revision,
                reason=reason,
            )
        return _instance(row)

    @service_method(read_only=False, since="0.6.0")
    def resume(self, *, actor: AbstractBaseUser, instance_id: UUID, expected_revision: int) -> InstanceInfo:
        """Resume a suspended instance and reschedule what its steps were waiting for.

        Autorização: o administrador da organização.
        Erros: NotFound; Forbidden; Conflict(stale_revision); Conflict(not_suspended); Unavailable.
        Efeitos: muda o estado e reenfileira os jobs pendentes.
        """
        from skeleton.flows import services

        with _flow_errors():
            row = services.resume_instance(
                as_uuid(instance_id, field="instance_id"), actor=actor, expected_revision=expected_revision
            )
        return _instance(row)

    @service_method(read_only=False, since="0.6.0")
    def retry(self, *, actor: AbstractBaseUser, instance_id: UUID, expected_revision: int) -> InstanceInfo:
        """Put a failed instance back to work from the step that failed.

        Autorização: o administrador da organização.
        Erros: NotFound; Forbidden; Conflict(stale_revision); Conflict(not_failed); Unavailable.
        Efeitos: reativa as etapas com falha e reenfileira os jobs delas.
        """
        from skeleton.flows import services

        with _flow_errors():
            row = services.retry_instance(
                as_uuid(instance_id, field="instance_id"), actor=actor, expected_revision=expected_revision
            )
        return _instance(row)
