from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from rest_framework.response import Response

from skeleton.api.openapi import INTEGER, Operation, nullable, obj, string
from skeleton.api.views import TenantAPI, request_fields
from skeleton.service_api import Conflict
from skeleton.service_api.applications import ApplicationService
from skeleton.web.views import tenant_page

from .service_api import NoteService


@tenant_page()
def home(request, organization_id):
    service = NoteService()
    count = service.note_count(actor=request.user)
    configuration = ApplicationService().my_configuration(actor=request.user, app_label="example_notes")
    notes = service.list_notes(actor=request.user, page_size=10).items
    return render(
        request,
        "example_notes/home.html",
        {"heading": configuration.get("heading"), "count": count, "organization_id": organization_id, "notes": notes},
    )


@tenant_page()
@require_POST
@transaction.atomic
def request_publication(request, organization_id, note_id):
    """Starts the publication flow; the request then appears in the author's task inbox."""
    try:
        with transaction.atomic():
            NoteService().request_publication(actor=request.user, note_id=note_id)
    except Conflict:
        messages.error(request, "Esta anotação já tem um pedido de publicação em andamento.")
        return redirect("example_notes:home", organization_id=organization_id)
    messages.success(request, "Pedido de publicação iniciado. Preencha-o na sua caixa de tarefas.")
    return redirect("flows:tasks", organization_id=organization_id)


class SummaryView(TenantAPI):
    # Documented in /api/v1/openapi.json (#178) for actors where the app is active.
    openapi = {"GET": Operation("Resumo das anotações", obj({"count": INTEGER, "heading": nullable(string())}))}

    def get(self, request, organization_id):
        count = NoteService().note_count(actor=request.user)
        configuration = ApplicationService().my_configuration(actor=request.user, app_label="example_notes")
        return Response({"count": count, "heading": configuration.get("heading")})


class PublicationView(TenantAPI):
    """``POST {"note": "<uuid>"}`` starts the publication flow; ``GET ?note=<uuid>`` reads its state."""

    def get(self, request, organization_id):
        status = NoteService().publication_status(actor=request.user, note_id=request.query_params.get("note"))
        return Response({"publication": _status(status)})

    @transaction.atomic
    def post(self, request, organization_id):
        data = request_fields(request)
        status = NoteService().request_publication(
            actor=request.user, note_id=data.get("note"), idempotency_key=data.get("idempotency_key")
        )
        return Response({"publication": _status(status)}, status=201 if status.created else 200)


def _status(status):
    if status is None:
        return None
    return {
        "instance": status.instance_id,
        "number": status.number,
        "status": status.status,
        "outcome": status.outcome,
        "revision": status.revision,
    }
