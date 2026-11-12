from django.shortcuts import render
from rest_framework.response import Response

from skeleton.api.openapi import INTEGER, Operation, nullable, obj, string
from skeleton.api.views import TenantAPI
from skeleton.service_api.applications import ApplicationService
from skeleton.web.views import tenant_page

from .service_api import NoteService


@tenant_page()
def home(request, organization_id):
    count = NoteService().note_count(actor=request.user)
    configuration = ApplicationService().my_configuration(actor=request.user, app_label="example_notes")
    return render(
        request,
        "example_notes/home.html",
        {"heading": configuration.get("heading"), "count": count, "organization_id": organization_id},
    )


class SummaryView(TenantAPI):
    # Documented in /api/v1/openapi.json (#178) for actors where the app is active.
    openapi = {"GET": Operation("Resumo das anotações", obj({"count": INTEGER, "heading": nullable(string())}))}

    def get(self, request, organization_id):
        count = NoteService().note_count(actor=request.user)
        configuration = ApplicationService().my_configuration(actor=request.user, app_label="example_notes")
        return Response({"count": count, "heading": configuration.get("heading")})
