from django.urls import path

from . import user_views

app_name = "skeleton_calendar_me"
urlpatterns = [path("", user_views.my_agenda, name="mine")]
