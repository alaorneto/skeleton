"""Core routing, with project-owned routes supplied by the entrypoint."""
from django.contrib import admin
from django.urls import include, path

from skeleton.accounts import oidc
from skeleton.accounts.views import invitation_view, logout
from skeleton.system.app_registry import application_urlpatterns


def application_urls(project_urls=()):
    routes = application_urlpatterns() + [
        path("accounts/oidc/<str:provider_id>/login/", oidc.login, name="openid_connect_login"),
        path("accounts/oidc/<str:provider_id>/login/callback/", oidc.callback, name="openid_connect_callback"),
        path("accounts/logout/", logout, name="account_logout"),
        path("accounts/", include("allauth.urls")),
        path("admin/", admin.site.urls),
        path("invite/<uuid:organization_id>/<str:token>/", invitation_view, name="invitation"),
        path("", include("skeleton.system.application_urls")),
    ]
    if project_urls:
        routes.append(path("", include(project_urls)))
    routes.extend([
        path("api/v1/", include("skeleton.api.urls")),
        path("", include("skeleton.web.urls")),
    ])
    return routes
