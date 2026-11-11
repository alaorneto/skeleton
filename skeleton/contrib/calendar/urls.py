from django.urls import path

from . import views

app_name = "skeleton_calendar"
urlpatterns = [
    path("", views.home, name="home"),
    path("events/new/", views.event_new, name="event-new"),
    path("events/<uuid:sysid>/cancel/", views.event_cancel, name="event-cancel"),
    path("member-access/", views.member_access, name="member-access"),
    path("calendars/<uuid:sysid>.ics", views.calendar_ics, name="calendar-ics"),
]
