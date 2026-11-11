from django.urls import path

from . import views

app_name = "skeleton_calendar"
urlpatterns = [
    path("", views.home, name="home"),
    path("member-access/", views.member_access, name="member-access"),
]
