from django.urls import path

from .views import home, request_publication

app_name = "example_notes"
urlpatterns = [
    path("", home, name="home"),
    path("notes/<uuid:note_id>/publication/", request_publication, name="request-publication"),
]
