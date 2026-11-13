"""REST of the process flows under ``/api/v1/o/<org>/flows/`` (docs/api.md, "Fluxos de processo").

Thin over ``FlowService``: the service authorizes every call. Since #410, API tokens reach the
tasks and instances with the scopes ``flows:read`` (reads) and ``flows:write`` (commands); the
operations panel and the drafts stay session only, because tokens never carry a capability. Commands require ``If-Match`` with the revision of the task or of the
instance and answer 412 when it is stale, like the other core resources.

Every route is documented in ``/api/v1/openapi.json`` (#178). The response schemas are derived
from the ``FlowService`` DTOs, so they cannot drift from what ``asdict`` renders; each action has
its own literal route and view class, because the schema leaves regex routes out.
"""

import dataclasses
import types
import typing
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils.translation import gettext
from rest_framework.response import Response

from skeleton.access.capabilities import FLOWS_MANAGE
from skeleton.access.policies import require_credential_scope
from skeleton.api.errors import BoundaryError, if_match_precondition, parse_revision
from skeleton.api.openapi import (
    BOOLEAN,
    DATETIME,
    INTEGER,
    OBJECT,
    Operation,
    Schema,
    array,
    nullable,
    obj,
    string,
)
from skeleton.api.openapi import UUID as UUID_SCHEMA
from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.service_api.flows import ActivityEntry, FlowService, InstanceInfo, TaskDetail, TaskSummary

FLOWS_READ = "flows:read"
FLOWS_WRITE = "flows:write"
TASK_ACTIONS = ("claim", "release", "reassign", "complete", "decide")
INSTANCE_ACTIONS = ("cancel", "suspend", "resume", "retry")
_PRIMITIVES: dict[Any, Schema] = {str: string(), int: INTEGER, bool: BOOLEAN, UUID: UUID_SCHEMA, datetime: DATETIME}
_JSON_TYPES = {str: "string", int: "integer", bool: "boolean"}


def dto_schema(annotation: Any) -> Schema:
    """JSON Schema of a DTO as ``asdict`` and the DRF encoder render it."""
    if dataclasses.is_dataclass(annotation):
        hints = typing.get_type_hints(annotation)
        return obj({item.name: dto_schema(hints[item.name]) for item in dataclasses.fields(annotation)})
    origin, arguments = typing.get_origin(annotation), typing.get_args(annotation)
    if origin is tuple:
        return array(dto_schema(arguments[0]))
    if origin in (types.UnionType, typing.Union):
        members = [item for item in arguments if item is not type(None)]
        optional = len(members) < len(arguments)
        if len(members) == 1:
            schema = dto_schema(members[0])
        else:  # A scalar form value: str | int | bool.
            schema = {"type": [_JSON_TYPES[item] for item in members]}
        return nullable(schema) if optional else schema
    return dict(_PRIMITIVES[annotation])


def _cursor_page(item: Any) -> Schema:
    return obj(
        {
            "results": array(dto_schema(item)),
            "next_cursor": nullable(string(description="Cursor da página seguinte; null na última.")),
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
        }
    )


_PAGING = (
    ("cursor", string(), "Cursor devolvido em next_cursor."),
    ("page_size", {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}, "Itens por página."),
)
_FORM_BODY = {
    "data": {**OBJECT, "description": "Valores dos campos do formulário da etapa e da transição."},
    "comment": string(),
}


def _filters(request, allowed):
    unknown = set(request.query_params) - set(allowed) - {"cursor", "page_size"}
    if unknown:
        raise BoundaryError("invalid_filter", gettext("Filtro não permitido."))
    values = {name: request.query_params[name] for name in allowed if name in request.query_params}
    if "cursor" in request.query_params:
        values["cursor"] = request.query_params["cursor"]
    if "page_size" in request.query_params:
        try:
            values["page_size"] = int(request.query_params["page_size"])
        except ValueError:
            raise BoundaryError("invalid_pagination", gettext("Página ou tamanho inválido.")) from None
    return values


def _page(page):
    return Response(
        {"results": [asdict(item) for item in page.items], "next_cursor": page.next_cursor, "page_size": page.page_size}
    )


def _body(request, allowed):
    data = request_fields(request)
    unknown = set(data) - set(allowed)
    if unknown:
        raise BoundaryError(
            "invalid_request",
            gettext("Campo não permitido."),
            fields={name: [gettext("Campo não permitido.")] for name in unknown},
        )
    return data


def _form_data(data):
    values = data.get("data", {})
    if not isinstance(values, dict):
        raise BoundaryError("invalid_request", gettext("data é um objeto de campos do formulário."))
    return tuple(values.items())


class TasksView(TenantAPI):
    token_methods = ("GET",)
    openapi = {
        "GET": Operation(
            "Caixa de tarefas do ator",
            lambda: _cursor_page(TaskSummary),
            scope=FLOWS_READ,
            query=(
                (
                    "scope",
                    string(enum=["mine", "available", "all"]),
                    "Minhas (padrão), das filas ou todas.",
                ),
                ("status", string(), "Situação da tarefa; só com scope=all."),
                *_PAGING,
            ),
        )
    }

    def get(self, request, organization_id):
        require_credential_scope(FLOWS_READ)
        return _page(FlowService().tasks(actor=request.user, **_filters(request, ("scope", "status"))))


class TaskView(TenantAPI):
    token_methods = ("GET",)
    openapi = {
        "GET": Operation(
            "Tarefa com formulário, ações e dados do processo",
            lambda: dto_schema(TaskDetail),
            etag=True,
            scope=FLOWS_READ,
        )
    }

    def get(self, request, organization_id, sysid):
        require_credential_scope(FLOWS_READ)
        detail = FlowService().task(actor=request.user, task_id=sysid)
        return etag(Response(asdict(detail)), detail.task.revision)


class TaskActionView(TenantAPI):
    """The commands on a task; each action is routed to a subclass documenting it."""

    token_methods = ("POST",)

    @transaction.atomic
    def post(self, request, organization_id, sysid, action):
        require_credential_scope(FLOWS_WRITE)
        revision = parse_revision(request.headers.get("If-Match"))
        service, actor = FlowService(), request.user
        with if_match_precondition():
            if action == "claim":
                _body(request, ())
                task = service.claim_task(actor=actor, task_id=sysid, expected_revision=revision)
            elif action == "release":
                _body(request, ())
                task = service.release_task(actor=actor, task_id=sysid, expected_revision=revision)
            elif action == "reassign":
                data = _body(request, ("to_user", "comment"))
                task = service.reassign_task(
                    actor=actor,
                    task_id=sysid,
                    expected_revision=revision,
                    to_user_id=data.get("to_user"),
                    comment=data.get("comment", ""),
                )
            elif action == "complete":
                data = _body(request, ("action", "data", "comment"))
                task = service.complete_task(
                    actor=actor,
                    task_id=sysid,
                    expected_revision=revision,
                    action=data.get("action"),
                    data=_form_data(data),
                    comment=data.get("comment", ""),
                )
            else:
                data = _body(request, ("decision", "data", "comment"))
                task = service.decide_task(
                    actor=actor,
                    task_id=sysid,
                    expected_revision=revision,
                    decision=data.get("decision"),
                    data=_form_data(data),
                    comment=data.get("comment", ""),
                )
        return etag(Response(asdict(task)), task.revision)


def _task_command(summary: str, request: Schema | None = None) -> Operation:
    return Operation(
        summary,
        lambda: dto_schema(TaskSummary),
        request=request,
        request_required=False,
        etag=True,
        if_match=True,
        scope=FLOWS_WRITE,
    )


TASK_ACTION_OPERATIONS = {
    "claim": _task_command("Assume uma tarefa de fila"),
    "release": _task_command("Devolve uma tarefa à fila"),
    "reassign": _task_command(
        "Reatribui ou delega a tarefa",
        obj({"to_user": UUID_SCHEMA, "comment": string()}, required=("to_user",)),
    ),
    "complete": _task_command(
        "Conclui a tarefa por uma das ações", obj({"action": string(), **_FORM_BODY}, required=())
    ),
    "decide": _task_command(
        "Registra a decisão de uma aprovação",
        obj({"decision": string(enum=["approved", "rejected"]), **_FORM_BODY}, required=("decision",)),
    ),
}


def _draft_schema() -> Schema:
    return obj(
        {
            "sysid": UUID_SCHEMA,
            "revision": INTEGER,
            "key": string(),
            "label": string(),
            "app_label": string(),
            "specification": {**OBJECT, "description": "Forma JSON de FlowSpec, sem a versão."},
            "published_version": nullable(INTEGER),
            "published_at": nullable(DATETIME),
            "problems": array(string()),
        }
    )


_DRAFT_BODY = obj({"specification": {**OBJECT, "description": "Forma JSON de FlowSpec, sem a versão."}})


def _draft_errors():
    """Present the draft service errors as the core resources do."""
    import contextlib

    from django.core.exceptions import ValidationError
    from django.http import Http404

    from skeleton.common.exceptions import ConcurrentUpdate

    from .models import FlowDraft

    @contextlib.contextmanager
    def handler():
        try:
            yield
        except FlowDraft.DoesNotExist:
            raise Http404 from None
        except ConcurrentUpdate:
            raise BoundaryError(
                "revision_conflict", gettext("O rascunho foi alterado por outra operação."), 412
            ) from None
        except ValidationError as error:
            messages = (
                error.message_dict.get("specification", error.messages)
                if hasattr(error, "message_dict")
                else error.messages
            )
            raise BoundaryError(
                "invalid_request", gettext("Rascunho inválido."), fields={"specification": list(messages)}
            ) from None

    return handler()


class DraftsView(TenantAPI):
    """Flows authored as data (#414): list and create drafts."""

    openapi = {
        "GET": Operation(
            "Rascunhos de fluxo da organização",
            lambda: obj({"results": array(_draft_schema())}),
            capability=FLOWS_MANAGE,
        ),
        "POST": Operation(
            "Cria um rascunho de fluxo",
            _draft_schema,
            status=201,
            request=_DRAFT_BODY,
            etag=True,
            capability=FLOWS_MANAGE,
        ),
    }

    def get(self, request, organization_id):
        from .definitions import drafts

        return Response({"results": [asdict(item) for item in drafts(request.user)]})

    @transaction.atomic
    def post(self, request, organization_id):
        from .definitions import save_draft

        data = _body(request, ("specification",))
        with _draft_errors():
            created = save_draft(request.user, specification=data.get("specification"))
        return etag(Response(asdict(created), status=201), created.revision)


class DraftView(TenantAPI):
    openapi = {
        "GET": Operation("Rascunho com os problemas de validação", _draft_schema, etag=True, capability=FLOWS_MANAGE),
        "PATCH": Operation(
            "Troca a especificação do rascunho",
            _draft_schema,
            request=_DRAFT_BODY,
            etag=True,
            if_match=True,
            capability=FLOWS_MANAGE,
        ),
    }

    def get(self, request, organization_id, sysid):
        from .definitions import draft

        with _draft_errors():
            item = draft(request.user, sysid)
        return etag(Response(asdict(item)), item.revision)

    @transaction.atomic
    def patch(self, request, organization_id, sysid):
        from .definitions import save_draft

        revision = parse_revision(request.headers.get("If-Match"))
        data = _body(request, ("specification",))
        with _draft_errors():
            item = save_draft(
                request.user, specification=data.get("specification"), draft_id=sysid, expected_revision=revision
            )
        return etag(Response(asdict(item)), item.revision)


class DraftPublishView(TenantAPI):
    openapi = {
        "POST": Operation(
            "Publica o rascunho como a próxima versão do fluxo",
            _draft_schema,
            etag=True,
            if_match=True,
            capability=FLOWS_MANAGE,
            request_required=False,
        )
    }

    @transaction.atomic
    def post(self, request, organization_id, sysid):
        from .definitions import publish_draft

        revision = parse_revision(request.headers.get("If-Match"))
        _body(request, ())
        with _draft_errors():
            item = publish_draft(request.user, draft_id=sysid, expected_revision=revision)
        return etag(Response(asdict(item)), item.revision)


def _migration_errors():
    import contextlib

    from django.core.exceptions import ValidationError

    @contextlib.contextmanager
    def handler():
        try:
            yield
        except ValidationError as error:
            raise BoundaryError("invalid_request", " ".join(error.messages)) from None

    return handler()


def _migration_item_schema() -> Schema:
    return obj(
        {
            "instance_id": UUID_SCHEMA,
            "number": string(),
            "status": string(),
            "moves": array(array(string())),
            "skipped": string(description="Vazio quando a instância migra; senão o motivo."),
        }
    )


class MigrationView(TenantAPI):
    """Move running instances to the declared new version (#413): preview, then apply by job."""

    openapi = {
        "GET": Operation(
            "Prévia da migração das instâncias de uma versão",
            lambda: obj({"results": array(_migration_item_schema())}),
            capability=FLOWS_MANAGE,
        ),
        "POST": Operation(
            "Migra as instâncias da versão, por job",
            lambda: obj({"job_id": UUID_SCHEMA}),
            status=202,
            capability=FLOWS_MANAGE,
            request_required=False,
        ),
    }

    def get(self, request, organization_id, flow_key, from_version):
        from .migration import preview

        with _migration_errors():
            items = preview(request.user, flow_key=flow_key, from_version=from_version)
        return Response({"results": [asdict(item) for item in items]})

    @transaction.atomic
    def post(self, request, organization_id, flow_key, from_version):
        from .migration import request_migration

        _body(request, ())
        with _migration_errors():
            receipt = request_migration(request.user, flow_key=flow_key, from_version=from_version)
        return Response({"job_id": str(receipt.id)}, status=202)


class OperationsView(TenantAPI):
    """Operations panel (#408), for who administers flows."""

    openapi = {
        "GET": Operation(
            "Painel de operação dos fluxos da organização",
            lambda: dto_schema(_operations_overview()),
            capability=FLOWS_MANAGE,
            query=(("flow_key", string(), "Chave do fluxo."), ("version", INTEGER, "Versão do fluxo.")),
        )
    }

    def get(self, request, organization_id):
        from .operations import overview

        version = request.query_params.get("version")
        try:
            flow_version = int(version) if version not in (None, "") else None
        except ValueError:
            raise BoundaryError("invalid_request", gettext("Versão inválida.")) from None
        panel = overview(
            request.user, flow_key=(request.query_params.get("flow_key") or "")[:100], flow_version=flow_version
        )
        return Response(asdict(panel))


def _operations_overview():
    from .operations import OperationsOverview

    return OperationsOverview


class InstancesView(TenantAPI):
    token_methods = ("GET",)
    openapi = {
        "GET": Operation(
            "Instâncias de fluxo visíveis ao ator",
            lambda: _cursor_page(InstanceInfo),
            scope=FLOWS_READ,
            query=(
                ("flow_key", string(), "Chave do fluxo."),
                ("subject_id", UUID_SCHEMA, "Registro do processo."),
                ("status", string(), "Situação da instância."),
                *_PAGING,
            ),
        )
    }

    def get(self, request, organization_id):
        require_credential_scope(FLOWS_READ)
        filters = _filters(request, ("flow_key", "subject_id", "status"))
        return _page(FlowService().instances(actor=request.user, **filters))


class InstanceView(TenantAPI):
    token_methods = ("GET",)
    openapi = {
        "GET": Operation(
            "Instância com etapas ativas e dados não sensíveis",
            lambda: dto_schema(InstanceInfo),
            etag=True,
            scope=FLOWS_READ,
        )
    }

    def get(self, request, organization_id, sysid):
        require_credential_scope(FLOWS_READ)
        instance = FlowService().instance(actor=request.user, instance_id=sysid)
        return etag(Response(asdict(instance)), instance.revision)


class InstanceHistoryView(TenantAPI):
    token_methods = ("GET",)
    openapi = {
        "GET": Operation("Histórico da instância", lambda: _cursor_page(ActivityEntry), query=_PAGING, scope=FLOWS_READ)
    }

    def get(self, request, organization_id, sysid):
        require_credential_scope(FLOWS_READ)
        return _page(FlowService().history(actor=request.user, instance_id=sysid, **_filters(request, ())))


class InstanceActionView(TenantAPI):
    """The administration of an instance; each action is routed to a subclass documenting it."""

    token_methods = ("POST",)

    @transaction.atomic
    def post(self, request, organization_id, sysid, action):
        require_credential_scope(FLOWS_WRITE)
        revision = parse_revision(request.headers.get("If-Match"))
        service = FlowService()
        data = _body(request, ("reason",) if action in ("cancel", "suspend") else ())
        with if_match_precondition():
            if action in ("cancel", "suspend"):
                instance = getattr(service, action)(
                    actor=request.user, instance_id=sysid, expected_revision=revision, reason=data.get("reason", "")
                )
            else:
                instance = getattr(service, action)(actor=request.user, instance_id=sysid, expected_revision=revision)
        return etag(Response(asdict(instance)), instance.revision)


def _instance_command(summary: str, *, reason: bool = False, capability: str | None = None) -> Operation:
    return Operation(
        summary,
        lambda: dto_schema(InstanceInfo),
        request=obj({"reason": string()}, required=()) if reason else None,
        request_required=False,
        etag=True,
        if_match=True,
        capability=capability,
        scope=FLOWS_WRITE,
    )


INSTANCE_ACTION_OPERATIONS = {
    "cancel": _instance_command(
        "Cancela a instância (administrador, ou quem iniciou se o fluxo permitir)", reason=True
    ),
    "suspend": _instance_command("Suspende a instância", reason=True, capability=FLOWS_MANAGE),
    "resume": _instance_command("Retoma a instância suspensa", capability=FLOWS_MANAGE),
    "retry": _instance_command("Repete a etapa que falhou", capability=FLOWS_MANAGE),
}


def _action_view(base: type, action: str, operation: Operation) -> type:
    name = f"{action.title()}{base.__name__}"
    return type(name, (base,), {"__module__": __name__, "__qualname__": name, "openapi": {"POST": operation}})


TASK_ACTION_VIEWS = {
    action: _action_view(TaskActionView, action, TASK_ACTION_OPERATIONS[action]) for action in TASK_ACTIONS
}
INSTANCE_ACTION_VIEWS = {
    action: _action_view(InstanceActionView, action, INSTANCE_ACTION_OPERATIONS[action]) for action in INSTANCE_ACTIONS
}
