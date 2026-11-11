from django.urls import path

from . import feed, user_views

app_name = "skeleton_calendar_me"
urlpatterns = [
    path("", user_views.my_agenda, name="mine"),
    path("feed-token/", feed.feed_token, name="feed-token"),
    path("feed/<str:token>.ics", feed.feed, name="feed"),
]
