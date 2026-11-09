from django.shortcuts import render
from rest_framework.response import Response

from skeleton.api.views import TenantAPI
from skeleton.system.applications import application_state
from skeleton.web.views import tenant_page

from .service_api import NoteService


@tenant_page()
def home(request, organization_id):
    count = NoteService().note_count(actor=request.user)
    configuration = application_state("example_notes")["configuration"]
    return render(
        request,
        "example_notes/home.html",
        {"heading": configuration["heading"], "count": count, "organization_id": organization_id},
    )


class SummaryView(TenantAPI):
    def get(self, request, organization_id):
        count = NoteService().note_count(actor=request.user)
        return Response({"count": count, "heading": application_state("example_notes")["configuration"]["heading"]})
