"""Pages of the process flows under ``/o/<org>/flows/``: task inbox, task, instances and history.

Thin over ``FlowService`` and usable without JavaScript. ``tenant_page`` maps the service
errors (missing or hidden: 404; forbidden: 403); a stale revision answers 409 and a form the
service refuses is shown again with its errors.
"""

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.service_api import errors
from skeleton.service_api.flows import FlowService
from skeleton.web.views import tenant_page

TEMPLATES = "skeleton/flows/"
SCOPES = {
    "mine": gettext_lazy("Minhas tarefas"),
    "available": gettext_lazy("Disponíveis para mim"),
    "all": gettext_lazy("Todas que posso ver"),
}
STALE = gettext_lazy("Os dados mudaram desde que a página foi aberta. Confira o estado atual e tente de novo.")


def _cursor_page(request, fetch):
    cursor = request.GET.get("cursor") or None
    page = fetch(cursor)
    return {"items": page.items, "next_cursor": page.next_cursor}


def _django_field(info):
    label, help_text = info.label, info.help_text
    if info.type == "bool":
        return forms.BooleanField(label=label, required=False, help_text=help_text)
    if info.type == "choice":
        choices = [("", "—"), *((choice, choice) for choice in info.choices)]
        return forms.ChoiceField(label=label, required=False, choices=choices, help_text=help_text)
    if info.type == "text":
        return forms.CharField(
            label=label, required=False, widget=forms.Textarea(attrs={"rows": 4}), help_text=help_text
        )
    widgets = {"date": forms.DateInput(attrs={"type": "date"}), "int": forms.NumberInput()}
    if info.type == "datetime" and not help_text:
        help_text = gettext("Data e hora com fuso, no formato ISO 8601 (2026-12-31T18:00:00-03:00).")
    if info.type == "user" and not help_text:
        help_text = gettext("Identificador (UUID) de uma pessoa com vínculo ativo.")
    return forms.CharField(label=label, required=False, widget=widgets.get(info.type), help_text=help_text)


class TaskForm(forms.Form):
    """Presentation only: the declared fields as inputs; the service validates everything."""

    def __init__(self, *args, infos=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.infos = {info.name: info for info in infos}
        for info in infos:
            self.fields[info.name] = _django_field(info)
            self.fields[info.name].label = info.label + (" *" if info.required else "")
            if info.value is not None:
                self.fields[info.name].initial = info.value
        self.fields["comment"] = forms.CharField(
            label=gettext("Comentário"), required=False, widget=forms.Textarea(attrs={"rows": 3}), max_length=4000
        )

    def service_data(self):
        """Raw values of the declared fields that were submitted, for the service to validate."""
        values = []
        for name, info in self.infos.items():
            if info.type == "bool":
                values.append((name, bool(self.cleaned_data.get(name))))
            elif name in self.data:
                values.append((name, self.cleaned_data.get(name) or None))
        return tuple(values)

    def apply(self, error):
        for name, problems in error.errors.items():
            self.add_error(name if name in self.fields else None, list(problems))


def _revision(request):
    try:
        return int(request.POST.get("revision", ""))
    except ValueError:
        return 0


@tenant_page()
def task_list(request, organization_id):
    scope = request.GET.get("scope", "mine")
    if scope not in SCOPES:
        scope = "mine"
    page = _cursor_page(request, lambda cursor: FlowService().tasks(actor=request.user, scope=scope, cursor=cursor))
    return render(
        request,
        TEMPLATES + "tasks.html",
        {"organization_id": organization_id, "scope": scope, "scopes": SCOPES, "page": page},
    )


def _action_fields(detail, action_key):
    chosen = next((item for item in detail.actions if item.key == action_key), None)
    names = {field.name for field in detail.fields}
    extra = [field for field in (chosen.fields if chosen else ()) if field.name not in names]
    return [*detail.fields, *extra]


@tenant_page()
@require_http_methods(["GET", "POST"])
@transaction.atomic
def task_detail(request, organization_id, sysid):
    service = FlowService()
    detail = service.task(actor=request.user, task_id=sysid)
    extra = {field.name: field for action in detail.actions for field in action.fields}
    infos = [*detail.fields, *(field for name, field in extra.items() if name not in {f.name for f in detail.fields})]
    form = TaskForm(request.POST or None, infos=infos if detail.can_act else ())
    status = 200
    if request.method == "POST":
        operation = request.POST.get("operation", "")
        try:
            with transaction.atomic():
                if operation == "claim":
                    service.claim_task(actor=request.user, task_id=sysid, expected_revision=_revision(request))
                elif operation == "release":
                    service.release_task(actor=request.user, task_id=sysid, expected_revision=_revision(request))
                elif operation == "reassign":
                    service.reassign_task(
                        actor=request.user,
                        task_id=sysid,
                        expected_revision=_revision(request),
                        to_user_id=request.POST.get("to_user", ""),
                        comment=request.POST.get("comment", ""),
                    )
                elif not form.is_valid():
                    raise errors.InvalidInput({})
                elif operation in ("approved", "rejected"):
                    service.decide_task(
                        actor=request.user,
                        task_id=sysid,
                        expected_revision=_revision(request),
                        decision=operation,
                        data=form.service_data(),
                        comment=form.cleaned_data["comment"],
                    )
                else:
                    wanted = {field.name for field in _action_fields(detail, operation)}
                    service.complete_task(
                        actor=request.user,
                        task_id=sysid,
                        expected_revision=_revision(request),
                        action=operation,
                        data=tuple(item for item in form.service_data() if item[0] in wanted),
                        comment=form.cleaned_data["comment"],
                    )
        except errors.InvalidInput as error:
            form.apply(error)
            status = 400
        except errors.Conflict as conflict:
            form.add_error(None, STALE if conflict.code == "stale_revision" else conflict.detail)
            status = 409
        else:
            messages.success(request, gettext("Tarefa atualizada."))
            if operation in ("claim", "release", "reassign"):
                return redirect("flows:task", organization_id=organization_id, sysid=sysid)
            return redirect("flows:tasks", organization_id=organization_id)
    return render(
        request,
        TEMPLATES + "task.html",
        {"organization_id": organization_id, "detail": detail, "task": detail.task, "form": form},
        status=status,
    )


def _version(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@tenant_page()
def operations(request, organization_id):
    """Operations panel (#408): failed instances and the open tasks that need someone."""
    from .operations import overview

    flow_key = (request.GET.get("flow_key") or "").strip()[:100]
    flow_version = _version(request.GET.get("version"))
    panel = overview(request.user, flow_key=flow_key, flow_version=flow_version)
    return render(
        request,
        TEMPLATES + "operations.html",
        {
            "organization_id": organization_id,
            "panel": panel,
            "flow_key": flow_key,
            "flow_version": flow_version or "",
        },
    )


@tenant_page()
def instance_list(request, organization_id):
    from .policies import is_flow_admin

    page = _cursor_page(request, lambda cursor: FlowService().instances(actor=request.user, cursor=cursor))
    return render(
        request,
        TEMPLATES + "instances.html",
        {"organization_id": organization_id, "page": page, "can_operate": is_flow_admin(request.user)},
    )


@tenant_page()
def instance_detail(request, organization_id, sysid):
    service = FlowService()
    instance = service.instance(actor=request.user, instance_id=sysid)
    history = service.history(actor=request.user, instance_id=sysid, page_size=100)
    from .policies import is_flow_admin

    return render(
        request,
        TEMPLATES + "instance.html",
        {
            "organization_id": organization_id,
            "instance": instance,
            "history": history.items,
            "more_history": history.next_cursor is not None,
            "is_admin": is_flow_admin(request.user),
            "is_starter": instance.starter_id == request.user.pk,
        },
    )


@tenant_page()
@require_POST
@transaction.atomic
def instance_action(request, organization_id, sysid, action):
    service = FlowService()
    arguments = {"actor": request.user, "instance_id": sysid, "expected_revision": _revision(request)}
    if action in ("cancel", "suspend"):
        arguments["reason"] = request.POST.get("reason", "")
    try:
        with transaction.atomic():
            getattr(service, action)(**arguments)
    except errors.Conflict as conflict:
        messages.error(request, STALE if conflict.code == "stale_revision" else conflict.detail)
    else:
        messages.success(request, gettext("Instância atualizada."))
    return redirect("flows:instance", organization_id=organization_id, sysid=sysid)
