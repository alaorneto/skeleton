from django.urls import path

from .views import PublicationView, SummaryView

app_name = "example_notes_api"
urlpatterns = [
    path("summary/", SummaryView.as_view(), name="summary"),
    path("publication/", PublicationView.as_view(), name="publication"),
]
