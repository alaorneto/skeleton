"""REST of the process flows under ``/api/v1/o/<org>/flows/`` (docs/api.md, "Fluxos de processo").

Thin over ``FlowService``: the service authorizes every call. Session only (no view declares
``token_methods``). Commands require ``If-Match`` with the revision of the task or of the
instance and answer 412 when it is stale, like the other core resources.
"""

from dataclasses import asdict

from django.db import transaction
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError, if_match_precondition, parse_revision
from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.service_api.flows import FlowService

TASK_ACTIONS = ("claim", "release", "reassign", "complete", "decide")
INSTANCE_ACTIONS = ("cancel", "suspend", "resume", "retry")


def _filters(request, allowed):
    unknown = set(request.query_params) - set(allowed) - {"cursor", "page_size"}
    if unknown:
        raise BoundaryError("invalid_filter", "Filtro não permitido.")
    values = {name: request.query_params[name] for name in allowed if name in request.query_params}
    if "cursor" in request.query_params:
        values["cursor"] = request.query_params["cursor"]
    if "page_size" in request.query_params:
        try:
            values["page_size"] = int(request.query_params["page_size"])
        except ValueError:
            raise BoundaryError("invalid_pagination", "Página ou tamanho inválido.") from None
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
            "invalid_request", "Campo não permitido.", fields={name: ["Campo não permitido."] for name in unknown}
        )
    return data


def _form_data(data):
    values = data.get("data", {})
    if not isinstance(values, dict):
        raise BoundaryError("invalid_request", "data é um objeto de campos do formulário.")
    return tuple(values.items())


class TasksView(TenantAPI):
    def get(self, request, organization_id):
        return _page(FlowService().tasks(actor=request.user, **_filters(request, ("scope", "status"))))


class TaskView(TenantAPI):
    def get(self, request, organization_id, sysid):
        detail = FlowService().task(actor=request.user, task_id=sysid)
        return etag(Response(asdict(detail)), detail.task.revision)


class TaskActionView(TenantAPI):
    @transaction.atomic
    def post(self, request, organization_id, sysid, action):
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


class InstancesView(TenantAPI):
    def get(self, request, organization_id):
        filters = _filters(request, ("flow_key", "subject_id", "status"))
        return _page(FlowService().instances(actor=request.user, **filters))


class InstanceView(TenantAPI):
    def get(self, request, organization_id, sysid):
        instance = FlowService().instance(actor=request.user, instance_id=sysid)
        return etag(Response(asdict(instance)), instance.revision)


class InstanceHistoryView(TenantAPI):
    def get(self, request, organization_id, sysid):
        return _page(FlowService().history(actor=request.user, instance_id=sysid, **_filters(request, ())))


class InstanceActionView(TenantAPI):
    @transaction.atomic
    def post(self, request, organization_id, sysid, action):
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
