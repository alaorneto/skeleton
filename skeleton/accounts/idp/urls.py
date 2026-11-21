"""Routes of the OIDC provider (#603) and of the connected devices page.

Every path of ``allauth.idp.urls`` is listed first with the core's wrapper, so allauth's own
views are reachable only through the policy of ``skeleton.accounts.idp.views``. The allauth
include stays after them for its URL names (``idp:oidc:*``), which allauth reverses.
"""

from django.urls import include, path

from . import views

urlpatterns = [
    path(".well-known/openid-configuration", views.configuration, name="oidc-configuration"),
    path(".well-known/jwks.json", views.jwks, name="oidc-jwks"),
    path("identity/o/authorize", views.authorize, name="oidc-authorize"),
    path("identity/o/device", views.not_offered),
    path("identity/o/logout", views.logout, name="oidc-logout"),
    path("identity/o/api/token", views.token, name="oidc-token"),
    path("identity/o/api/revoke", views.revoke, name="oidc-revoke"),
    path("identity/o/api/device/code", views.not_offered),
    path("identity/o/api/userinfo", views.userinfo, name="oidc-userinfo"),
    path("identity/o/api/introspect", views.not_offered),
    path("identity/o/api/clients", views.not_offered),
    path("profile/devices/", views.connected_devices, name="connected-devices"),
    path("profile/devices/<str:grant>/revoke/", views.connected_device_revoke, name="connected-device-revoke"),
    path("", include("allauth.idp.urls")),
]
