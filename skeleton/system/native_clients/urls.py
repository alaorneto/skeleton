"""Anonymous routes of the native client updates (#606), outside /api/v1/."""

from django.urls import path

from . import views

app_name = "native_clients"
urlpatterns = [
    path("clients/<str:client_id>/<str:channel>/<str:name>", views.client_file, name="file"),
]
