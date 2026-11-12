"""Core routing, with project-owned routes supplied by the entrypoint."""

from django.urls import include, path

from skeleton.accounts import oidc
from skeleton.accounts.ldap import views as ldap_views
from skeleton.accounts.views import invitation_view, logout
from skeleton.system.app_registry import application_urlpatterns


def application_urls(project_urls=()):
    routes = [
        path("", include("skeleton.system.application_urls")),
        *application_urlpatterns(),
        path("accounts/oidc/<str:provider_id>/login/", oidc.login, name="openid_connect_login"),
        path("accounts/oidc/<str:provider_id>/login/callback/", oidc.callback, name="openid_connect_callback"),
        path("accounts/ldap/<str:provider_id>/login/", ldap_views.login, name="ldap_login"),
        path("accounts/logout/", logout, name="account_logout"),
        path("accounts/", include("allauth.urls")),
        path("platform/", include("skeleton.web.platform.urls")),
        path("invite/<uuid:organization_id>/<str:token>/", invitation_view, name="invitation"),
    ]
    if project_urls:
        routes.append(path("", include(project_urls)))
    routes.append(path("", include("skeleton.notifications.urls")))
    routes.append(path("", include("skeleton.files.urls")))
    routes.append(path("", include("skeleton.comments.urls")))
    routes.append(path("", include("skeleton.transfer.urls")))
    # Before api/v1/: its generic o/<org>/<resource>/ would otherwise capture webhooks/.
    routes.append(path("", include("skeleton.integrations.urls")))
    # Process flows (docs/flows.md): also before api/v1/, for the same reason.
    routes.append(path("", include("skeleton.flows.urls")))
    routes.extend(
        [
            path("api/v1/", include("skeleton.api.urls")),
            path("", include("skeleton.web.urls")),
        ]
    )
    return routes
