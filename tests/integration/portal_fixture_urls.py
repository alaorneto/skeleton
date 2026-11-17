"""A test URLconf with the portal routes of a fixture app (#533), mounted like app_routing mounts a manifest."""

# Loaded only by tests marked needs_example_app: the fixture portal belongs to the example app.

from types import SimpleNamespace

from django.http import HttpResponse, JsonResponse
from django.urls import path
from django.views.decorators.http import require_http_methods
from rest_framework.response import Response

from config.urls import urlpatterns as core_urlpatterns
from skeleton.api.views import PortalAPI
from skeleton.service_api.external import ExternalAccessService
from skeleton.web.app_routing import portal_urlpatterns


@require_http_methods(["GET", "POST"])
def hello(request, organization_id):
    me = ExternalAccessService().me(actor=request.user)
    return HttpResponse(f"olá {me.user_id} {request.method}")


class MeAPI(PortalAPI):
    def get(self, request, organization_id):
        me = ExternalAccessService().me(actor=request.user)
        return Response({"user_id": str(me.user_id)})


def plain(request, organization_id):
    return JsonResponse({"ok": True})


FIXTURE = SimpleNamespace(
    app_label="example_notes",
    portal_urls=[path("", hello, name="home"), path("hello/", hello, name="hello")],
    portal_api_urls=[path("me/", MeAPI.as_view(), name="me")],
)
urlpatterns = [*portal_urlpatterns(FIXTURE), *core_urlpatterns]
