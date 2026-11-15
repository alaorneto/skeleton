from django.urls import path

from skeleton.tenancy.routing import ORG

from . import views

app_name = "files"
urlpatterns = [
    path(
        f"{ORG}tables/<str:table_key>/<uuid:sysid>/attachments/",
        views.upload,
        name="upload",
    ),
    path(
        f"{ORG}tables/<str:table_key>/<uuid:sysid>/attachments/<uuid:attachment_id>/download/",
        views.download,
        name="download",
    ),
    path(
        f"{ORG}tables/<str:table_key>/<uuid:sysid>/attachments/<uuid:attachment_id>/remove/",
        views.remove,
        name="remove",
    ),
]
