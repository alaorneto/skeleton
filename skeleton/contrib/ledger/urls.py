from django.urls import path

from . import views

app_name = "skeleton_ledger"
urlpatterns = [
    path("", views.home, name="home"),
    path("books/default/", views.default_book, name="default-book"),
    path("books/lock/", views.lock, name="lock"),
    path("member-access/", views.member_access, name="member-access"),
    path("accounts/", views.accounts, name="accounts"),
    path("accounts/chart/", views.apply_chart, name="apply-chart"),
    path("accounts/keys/", views.bind_key, name="bind-key"),
    path("accounts/<uuid:sysid>/active/", views.account_active, name="account-active"),
    path("entries/", views.entries, name="entries"),
    path("entries/new/", views.entry_new, name="entry-new"),
    path("entries/<uuid:sysid>/", views.entry_detail, name="entry"),
    path("entries/<uuid:sysid>/reverse/", views.entry_reverse, name="entry-reverse"),
    path("titles/", views.titles, name="titles"),
    path("titles/new/", views.title_new, name="title-new"),
    path("titles/<uuid:sysid>/", views.title_detail, name="title"),
    path("titles/<uuid:sysid>/cancel/", views.title_cancel, name="title-cancel"),
    path(
        "titles/<uuid:sysid>/settlements/<uuid:settlement_id>/reverse/",
        views.settlement_reverse,
        name="settlement-reverse",
    ),
    path("recurrences/", views.recurrences, name="recurrences"),
    path("recurrences/<uuid:sysid>/", views.recurrence_detail, name="recurrence"),
    path("recurrences/<uuid:sysid>/toggle/", views.recurrence_toggle, name="recurrence-toggle"),
    path("reports/", views.report, name="reports"),
    path("reports/<slug:name>/", views.report, name="report"),
]
