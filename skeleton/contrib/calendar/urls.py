from django.urls import path

from . import views

app_name = "skeleton_calendar"
urlpatterns = [
    path("", views.home, name="home"),
    path("events/new/", views.event_new, name="event-new"),
    path("events/<uuid:sysid>/cancel/", views.event_cancel, name="event-cancel"),
    path("member-access/", views.member_access, name="member-access"),
    path("calendars/<uuid:sysid>.ics", views.calendar_ics, name="calendar-ics"),
    path("calendars/", views.calendars, name="calendars"),
    path("calendars/<uuid:sysid>/", views.calendar_detail, name="calendar-detail"),
    path("calendars/<uuid:sysid>/rename/", views.calendar_rename, name="calendar-rename"),
    path("calendars/<uuid:sysid>/sharing/", views.calendar_sharing, name="calendar-sharing"),
    path("calendars/<uuid:sysid>/delete/", views.calendar_delete, name="calendar-delete"),
    path("calendars/<uuid:sysid>/link/", views.calendar_link, name="calendar-link"),
]
