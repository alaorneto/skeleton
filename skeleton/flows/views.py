"""Pages of the process flows under ``/o/<org>/flows/``: task inbox, task, instances and history.

Thin over ``FlowService`` and usable without JavaScript. ``tenant_page`` maps the service
errors (missing or hidden: 404; forbidden: 403); a stale revision answers 409 and a form the
service refuses is shown again with its errors.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

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


@login_required
@never_cache
@require_safe
def global_tasks(request):
    """Open tasks assigned to the person in every organization (#409).

    Like the global notification inbox: the middleware's global context of the person is what
    the self-discovery policy reads (an aggregate route would run without any actor).
    """
    from .inbox import open_tasks

    return render(request, TEMPLATES + "global_tasks.html", {"rows": open_tasks(request.user)})


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
        {
            "organization_id": organization_id,
            "detail": detail,
            "task": detail.task,
            "form": form,
            "subject_comments": _subject_comments(request, detail.subject_table, detail.subject_id),
        },
        status=status,
    )


def _subject_comments(request, subject_table, subject_id):
    """The business record's comment panel (#421), only for who reads the record.

    The authorization is the record's own (``readable_target``), as on the record page: who
    only has the task, without reading the record, sees no panel; a table without
    ``CommentPolicy`` shows none either.
    """
    from django.http import Http404

    from skeleton.comments.services import readable_target
    from skeleton.comments.views import panel_context
    from skeleton.system.registry import get_spec

    try:
        spec = get_spec(subject_table)
    except (KeyError, LookupError):
        return None
    if spec is None or getattr(spec, "comments", None) is None or not spec.expose_api:
        return None
    try:
        record = readable_target(spec, subject_id, user=request.user)
    except Http404:
        return None
    panel = panel_context(spec, record, user=request.user, next_url=request.path)
    if panel is None:
        return None
    return {"comments": panel, "table_key": subject_table, "record": {"sysid": record.pk}}


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


def _draft_steps(specification):
    """Rows for the editor's tables; tolerant of a half-written specification."""
    steps = specification.get("steps") if isinstance(specification, dict) else None
    transitions = specification.get("transitions") if isinstance(specification, dict) else None
    keep = lambda rows: [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []  # noqa: E731
    return keep(steps), keep(transitions)


def _draft_page(request, organization_id, *, item=None, text="", errors=(), status=200):
    import json

    from .registry import registered_names

    specification = item.specification if item is not None else {}
    steps, transitions = _draft_steps(specification)
    return render(
        request,
        TEMPLATES + "draft.html",
        {
            "organization_id": organization_id,
            "draft": item,
            "text": text or (json.dumps(specification, indent=2, ensure_ascii=False) if item is not None else ""),
            "errors": list(errors),
            "steps": steps,
            "transitions": transitions,
            "registered": registered_names(),
        },
        status=status,
    )


def _parse_specification(text):
    import json

    try:
        value = json.loads(text or "")
    except ValueError as error:
        return None, [gettext("JSON inválido: %(error)s") % {"error": error}]
    return value, []


def _draft_messages(error):
    if hasattr(error, "message_dict"):
        found = error.message_dict.get("specification")
        if found:
            return found
    return error.messages


@tenant_page()
@require_http_methods(["GET", "POST"])
@transaction.atomic
def draft_list(request, organization_id):
    """Flows authored as data (#415): the drafts, and a new one from a pasted specification."""
    from django.core.exceptions import ValidationError

    from . import definitions

    if request.method == "POST":
        specification, errors = _parse_specification(request.POST.get("specification"))
        if not errors:
            try:
                with transaction.atomic():  # A refused write rolls back to here; the page still renders.
                    created = definitions.save_draft(request.user, specification=specification)
            except ValidationError as error:
                errors = _draft_messages(error)
            else:
                return redirect("flows:draft", organization_id=organization_id, sysid=created.sysid)
        rows = definitions.drafts(request.user)
        return render(
            request,
            TEMPLATES + "drafts.html",
            {
                "organization_id": organization_id,
                "rows": rows,
                "errors": errors,
                "text": request.POST.get("specification", ""),
            },
            status=400,
        )
    rows = definitions.drafts(request.user)
    return render(
        request, TEMPLATES + "drafts.html", {"organization_id": organization_id, "rows": rows, "errors": [], "text": ""}
    )


@tenant_page()
@require_http_methods(["GET", "POST"])
@transaction.atomic
def draft_detail(request, organization_id, sysid):
    from django.core.exceptions import ValidationError
    from django.http import Http404

    from skeleton.common.exceptions import ConcurrentUpdate

    from . import definitions
    from .models import FlowDraft

    try:
        item = definitions.draft(request.user, sysid)
    except FlowDraft.DoesNotExist:
        raise Http404 from None
    if request.method == "GET":
        return _draft_page(request, organization_id, item=item)
    revision = _revision(request)
    text = request.POST.get("specification", "")
    try:
        if request.POST.get("intent") == "publish":
            with transaction.atomic():  # A refused publication rolls back to here; the page still renders.
                published = definitions.publish_draft(request.user, draft_id=sysid, expected_revision=revision)
            messages.success(
                request, gettext("Versão %(version)s publicada.") % {"version": published.published_version}
            )
            return redirect("flows:draft", organization_id=organization_id, sysid=sysid)
        specification, errors = _parse_specification(text)
        if errors:
            return _draft_page(request, organization_id, item=item, text=text, errors=errors, status=400)
        with transaction.atomic():
            definitions.save_draft(
                request.user, specification=specification, draft_id=sysid, expected_revision=revision
            )
    except ConcurrentUpdate:
        return _draft_page(request, organization_id, item=item, text=text, errors=[STALE], status=409)
    except ValidationError as error:
        return _draft_page(request, organization_id, item=item, text=text, errors=_draft_messages(error), status=400)
    messages.success(request, gettext("Rascunho salvo."))
    return redirect("flows:draft", organization_id=organization_id, sysid=sysid)


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
    from .diagram import for_instance
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
            # Read through FlowService above: the instance is visible to the person (#419).
            "diagram": for_instance(instance.sysid),
            "subject_comments": _subject_comments(request, instance.subject_table, instance.subject_id),
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
