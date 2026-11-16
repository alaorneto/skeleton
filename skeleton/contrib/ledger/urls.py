from django.urls import path

from . import views

app_name = "skeleton_ledger"
urlpatterns = [
    path("", views.home, name="home"),
    path("books/default/", views.default_book, name="default-book"),
    path("member-access/", views.member_access, name="member-access"),
]
