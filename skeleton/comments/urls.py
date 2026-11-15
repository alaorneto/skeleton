from django.urls import path

from skeleton.tenancy.routing import ORG

from . import views

app_name = "comments"
BASE = f"{ORG}tables/<str:table_key>/<uuid:sysid>/comments/"
urlpatterns = [
    path(BASE, views.add, name="add"),
    path(BASE + "<uuid:comment_id>/edit/", views.edit, name="edit"),
    path(BASE + "<uuid:comment_id>/remove/", views.remove, name="remove"),
    path(BASE + "<uuid:comment_id>/restore/", views.restore, name="restore"),
]
