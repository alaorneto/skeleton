from django.urls import path

from skeleton.tenancy.routing import ORG

from . import organization_views, preference_views, push_views, views

app_name = "notifications"
urlpatterns = [
    path("sw.js", push_views.service_worker, name="service-worker"),
    path("api/v1/me/push-subscriptions/", push_views.PushSubscriptionsAPI.as_view(), name="api-push-subscribe"),
    path(
        "api/v1/me/push-subscriptions/unsubscribe/",
        push_views.PushUnsubscribeAPI.as_view(),
        name="api-push-unsubscribe",
    ),
    path("profile/notifications/", preference_views.preferences_page, name="preferences"),
    path(
        "api/v1/me/notification-preferences/",
        preference_views.PreferencesAPI.as_view(),
        name="api-preferences",
    ),
    path(
        "api/v1/me/notification-preferences/<str:type_code>/<str:channel>/",
        preference_views.PreferenceAPI.as_view(),
        name="api-preference",
    ),
    path("notifications/", views.global_inbox, name="global"),
    path(f"{ORG}notifications/", views.organization_inbox, name="organization"),
    path(f"{ORG}notifications/read-all/", views.read_all, name="read-all"),
    path(f"{ORG}notifications/<uuid:sysid>/open/", views.open_notification, name="open"),
    path(f"{ORG}notifications/n/<uuid:notification_id>/", views.open_item, name="item"),
    path(
        f"{ORG}notifications/<uuid:sysid>/<str:action>/",
        views.change,
        name="change",
    ),
    # The organization's notifications, in its settings area (#785).
    path(f"{ORG}admin/notifications/", organization_views.settings_list, name="organization-settings"),
    path(
        f"{ORG}admin/notifications/<str:type_code>/",
        organization_views.settings_detail,
        name="organization-setting",
    ),
    path(
        f"{ORG}admin/notifications/<str:type_code>/texts/<str:language>/",
        organization_views.settings_text,
        name="organization-setting-text",
    ),
    path(
        f"{ORG}admin/notifications/<str:type_code>/texts/<str:language>/restore/",
        organization_views.settings_text_restore,
        name="organization-setting-text-restore",
    ),
    path(
        f"{ORG}admin/notifications/<str:type_code>/<slug:action>/",
        organization_views.settings_switch,
        name="organization-setting-switch",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/notification-settings/",
        organization_views.SettingsAPI.as_view(),
        name="api-organization-settings",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/notification-settings/<str:type_code>/",
        organization_views.SettingAPI.as_view(),
        name="api-organization-setting",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/notification-settings/<str:type_code>/texts/<str:language>/",
        organization_views.SettingTextAPI.as_view(),
        name="api-organization-setting-text",
    ),
    path("api/v1/notifications/", views.InboxAPI.as_view(), name="api-inbox"),
    path("api/v1/o/<uuid:organization_id>/notifications/actions/", views.InboxActionAPI.as_view(), name="api-actions"),
]
