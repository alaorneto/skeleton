from django.shortcuts import render
from rest_framework.response import Response

from skeleton.access.policies import authorized_queryset
from skeleton.api.views import TenantAPI
from skeleton.system.applications import application_state
from skeleton.web.views import tenant_page
from .models import Note


@tenant_page()
def home(request, organization_id):
    rows = authorized_queryset(Note, user=request.user)
    configuration = application_state('example_notes')['configuration']
    return render(request, 'example_notes/home.html', {'heading': configuration['heading'],
                  'count': rows.count(), 'organization_id': organization_id})


class SummaryView(TenantAPI):
    def get(self, request, organization_id):
        rows = authorized_queryset(Note, user=request.user)
        return Response({'count': rows.count(),
                         'heading': application_state('example_notes')['configuration']['heading']})
