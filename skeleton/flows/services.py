"""Instance lifecycle of the flow engine: start, signal, cancel, suspend, resume and retry.

Every command authorizes the actor itself, takes the instance row lock before reading
anything else and compares the revision after the lock. Nothing external happens here:
handlers run in jobs, and events and notifications are written in the caller's transaction.
``actor=None`` is accepted only inside a system context (jobs and event consumers).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context

from . import forms, policies
from .engine import Execution, lock_instance
from .errors import FlowConflict, FlowFailure
from .models import ACTIVE_STATES, FlowDefinition, FlowInstance, FlowStepRun
from .registry import get_flow
from .spec import FlowSpec


class NotVisible(ObjectDoesNotExist):
    """Missing, of another organization or not readable: indistinguishable on purpose."""


def _system_call(actor: Any) -> bool:
    context = current_context()
    if actor is None:
        if not context.system:
            raise PermissionDenied("An actor is required.")
        return True
    return False


def published_spec(flow_key: Any) -> FlowSpec:
    try:
        return get_flow(flow_key)
    except (KeyError, TypeError):
        raise ValidationError({"flow_key": "Fluxo não declarado."}) from None


def ensure_definition(spec: FlowSpec) -> FlowDefinition:
    """The frozen row of this version in the organization, written on first use.

    Changing a flow without bumping its version is a publication error: the stored digest
    no longer matches the code and nothing starts until the version changes.
    """
    organization_id = current_context().organization_id
    lookup = {"key": spec.key, "version": spec.version}
    definition = FlowDefinition.objects.filter(**lookup).first()
    if definition is None:
        try:
            with transaction.atomic():
                definition = FlowDefinition(
                    organization_id=organization_id,
                    app_label=spec.owner_app,
                    label=spec.label,
                    digest=spec.digest(),
                    specification=spec.to_json(),
                    **lookup,
                )
                definition.save()
        except (IntegrityError, ValidationError):
            definition = FlowDefinition.objects.filter(**lookup).first()
            if definition is None:
                raise
    if definition.digest != spec.digest():
        raise FlowConflict("definition_changed", "O fluxo mudou sem uma versão nova.")
    return definition


def _subject(spec: FlowSpec, subject_id: Any, actor: Any, *, system: bool) -> Any:
    from skeleton.access.policies import authorized_queryset, require_record_action
    from skeleton.system.registry import get_spec

    model = get_spec(spec.subject_table).model
    try:
        identifier = subject_id if isinstance(subject_id, UUID) else UUID(str(subject_id))
    except (ValueError, TypeError):
        raise ValidationError({"subject_id": "Identificador inválido."}) from None
    if system:
        rows = model.objects.all()
    else:
        try:
            rows = authorized_queryset(model, user=actor, action="read")
        except PermissionDenied:
            raise NotVisible() from None
    # Starts of the same record are serialized on its row: the second one sees the first instance.
    subject = rows.select_for_update().filter(pk=identifier).first()
    if subject is None:
        raise NotVisible()
    if not system:
        require_record_action(subject, user=actor, action=spec.start_action)
    return subject


def start_instance(
    flow_key: str, *, subject_id: Any, actor: Any, data: Any = None, idempotency_key: str | None = None
) -> tuple[FlowInstance, bool]:
    """Start the published version of a flow about a business record; ``(instance, created)``."""
    system = _system_call(actor)
    spec = published_spec(flow_key)
    policies.require_flow_app(spec.owner_app)
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200):
        raise ValidationError({"idempotency_key": "Chave de 1 a 200 caracteres."})
    subject = _subject(spec, subject_id, actor, system=system)
    values, sensitive = forms.clean((spec.start_form,), data)
    if idempotency_key is not None:
        repeated = FlowInstance.objects.filter(flow_key=spec.key, idempotency_key=idempotency_key).first()
        if repeated is not None:
            if repeated.subject_sysid != subject.pk:
                raise FlowConflict("idempotency_mismatch", "A chave já foi usada com outro registro.")
            return repeated, False
    definition = ensure_definition(spec)
    active = FlowInstance.objects.filter(flow_key=spec.key, subject_sysid=subject.pk, status__in=ACTIVE_STATES)
    if active.exists():
        raise FlowConflict("already_running", "Já existe uma instância ativa deste fluxo para o registro.")
    number = ""
    if spec.number_sequence:
        from skeleton.sequences.services import next_number

        number = next_number(spec.number_sequence).formatted[:64]
    try:
        with transaction.atomic():
            instance = FlowInstance(
                organization_id=current_context().organization_id,
                definition=definition,
                flow_key=spec.key,
                flow_version=spec.version,
                number=number,
                subject_table=spec.subject_table,
                subject_sysid=subject.pk,
                starter=None if system else actor,
                idempotency_key=idempotency_key,
            )
            instance.save()
            execution = Execution(instance)
            execution.log("instance_started", detail={"flow": spec.label, "version": spec.version})
            execution.submit("start", values, sensitive)
            execution.moments.append("started")
            execution.activate(execution.spec.start_step().key)
            execution.finish()
    except FlowFailure as failure:
        raise FlowConflict("flow_failed", f"O fluxo não pôde avançar ({failure.code}).") from None
    return instance, True


def _locked(instance_id: Any, actor: Any, *, system: bool = False) -> FlowInstance:
    """The locked instance, if its app is enabled and the actor may read it; else ``NotVisible``."""
    instance = lock_instance(instance_id)
    if instance is None:
        raise NotVisible()
    policies.require_flow_app(instance.definition.app_label)
    if not system and not policies.visible_instances(actor).filter(pk=instance.pk).exists():
        raise NotVisible()
    return instance


def _expect_revision(instance: FlowInstance, expected_revision: Any) -> None:
    if type(expected_revision) is not int:
        raise ValidationError({"expected_revision": "Use a revisão inteira atual."})
    if instance.revision != expected_revision:
        raise ConcurrentUpdate("The flow instance changed.")


def send_signal(instance_id: Any, name: str, *, actor: Any, data: Any = None) -> bool:
    """Deliver a signal to the waits of a running instance; ``False`` when nothing was waiting for it."""
    system = _system_call(actor)
    instance = _locked(instance_id, actor, system=system)
    if not isinstance(name, str) or not name:
        raise ValidationError({"name": "Informe o nome do sinal."})
    if instance.status == "suspended":
        raise FlowConflict("instance_suspended", "A instância está suspensa.")
    if instance.status != "running":
        return False
    execution = Execution(instance)
    waiting = [
        run
        for run in execution.open_runs()
        if run.kind == "wait" and run.status == "active" and execution.spec.step(run.step_key).signal == name
    ]
    if not waiting:
        return False
    try:
        with transaction.atomic():
            for run in waiting:
                step = execution.spec.step(run.step_key)
                values, sensitive = forms.clean((step.form,), data)
                execution.submit("signal", values, sensitive, run=run)
                execution.log("signal_received", run=run, detail={"signal": name})
                execution.complete(run, "received")
            execution.finish()
    except FlowFailure as failure:
        raise FlowConflict("flow_failed", f"O fluxo não pôde avançar ({failure.code}).") from None
    return True


def _require_admin(actor: Any) -> None:
    if not policies.is_flow_admin(actor):
        raise PermissionDenied("Flow administration required.")


def cancel_instance(instance_id: Any, *, actor: Any, expected_revision: int, reason: Any = "") -> FlowInstance:
    """Cancel an instance: the organization administrator, or the starter when the flow allows it."""
    instance = _locked(instance_id, actor)
    execution = Execution(instance)
    starter = instance.starter_id is not None and instance.starter_id == getattr(actor, "pk", None)
    if not (policies.is_flow_admin(actor) or (starter and execution.spec.starter_can_cancel)):
        raise PermissionDenied("Only the administrator or the starter cancels a flow.")
    comment = forms.clean_comment(reason)
    _expect_revision(instance, expected_revision)
    if instance.status not in (*ACTIVE_STATES, "failed"):
        raise FlowConflict("instance_closed", "A instância já foi encerrada.")
    # Failed runs stay open for a retry; a cancellation closes them too.
    FlowStepRun.objects.filter(instance=instance, status="failed").update(status="active")
    execution.cancel_runs()
    execution.touch(status="cancelled", finished_at=execution.now)
    execution.log("instance_cancelled", comment=comment)
    execution.moments.append("cancelled")
    execution.finish()
    return instance


def suspend_instance(instance_id: Any, *, actor: Any, expected_revision: int, reason: Any = "") -> FlowInstance:
    instance = _locked(instance_id, actor)
    _require_admin(actor)
    comment = forms.clean_comment(reason)
    _expect_revision(instance, expected_revision)
    if instance.status != "running":
        raise FlowConflict("not_running", "Só uma instância em andamento pode ser suspensa.")
    execution = Execution(instance)
    execution.touch(status="suspended")
    execution.log("instance_suspended", comment=comment)
    execution.finish()
    return instance


def resume_instance(instance_id: Any, *, actor: Any, expected_revision: int) -> FlowInstance:
    instance = _locked(instance_id, actor)
    _require_admin(actor)
    _expect_revision(instance, expected_revision)
    if instance.status != "suspended":
        raise FlowConflict("not_suspended", "A instância não está suspensa.")
    execution = Execution(instance)
    execution.touch(status="running")
    execution.log("instance_resumed")
    execution.resume_runs()
    execution.finish()
    return instance


def retry_instance(instance_id: Any, *, actor: Any, expected_revision: int) -> FlowInstance:
    """Put a failed instance back to work: failed steps become active and are scheduled again."""
    instance = _locked(instance_id, actor)
    _require_admin(actor)
    _expect_revision(instance, expected_revision)
    if instance.status != "failed":
        raise FlowConflict("not_failed", "A instância não está com falha.")
    execution = Execution(instance)
    for run in FlowStepRun.objects.filter(instance=instance, status="failed"):
        run.status = "active"
        run.save(update_fields={"status"})
    execution.touch(status="running")
    execution.log("instance_retried")
    execution.resume_runs()
    execution.finish()
    return instance
