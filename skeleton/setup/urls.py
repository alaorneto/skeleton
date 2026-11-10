"""URLconf of setup mode: only /setup/ and the health probes."""

from django.urls import include, path

from skeleton.api.health import probe_response

from . import views

steps = [
    path("setup/", views.start, name="start"),
    path("setup/instance/", views.instance, name="instance"),
    path("setup/database/", views.database_step, name="database"),
    path("setup/schema/", views.schema, name="schema"),
    path("setup/email/", views.email, name="email"),
    path("setup/storage/", views.storage, name="storage"),
    path("setup/admin/", views.admin, name="admin"),
    path("setup/finish/", views.finish, name="finish"),
]
urlpatterns = [
    path("", include((steps, "setup"))),
    path("health/live/", probe_response, name="health-live"),
    path("health/ready/", probe_response, name="health-ready"),
]
