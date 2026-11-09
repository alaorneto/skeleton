from django.urls import path

from . import views

app_name = "files"
urlpatterns = [
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/attachments/",
        views.upload,
        name="upload",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/attachments/<uuid:attachment_id>/remove/",
        views.remove,
        name="remove",
    ),
]
