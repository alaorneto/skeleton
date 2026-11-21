"""Core routing, with project-owned routes supplied by the entrypoint."""

from django.urls import include, path

from skeleton.accounts import oidc
from skeleton.accounts.ldap import views as ldap_views
from skeleton.accounts.views import external_invitation_view, invitation_view, logout
from skeleton.web.app_routing import application_urlpatterns


def application_urls(project_urls=()):
    routes = [
        path("", include("skeleton.web.application_urls")),
        path("", include("skeleton.api.application_urls")),
        path("", include("skeleton.web.billing_urls")),
        path("", include("skeleton.api.billing_urls")),
        *application_urlpatterns(),
        path("accounts/oidc/<str:provider_id>/login/", oidc.login, name="openid_connect_login"),
        path("accounts/oidc/<str:provider_id>/login/callback/", oidc.callback, name="openid_connect_callback"),
        path("accounts/ldap/<str:provider_id>/login/", ldap_views.login, name="ldap_login"),
        path("accounts/logout/", logout, name="account_logout"),
        path("accounts/", include("allauth.urls")),
        # OIDC provider for native clients and the connected devices page (#603).
        path("", include("skeleton.accounts.idp.urls")),
        # Anonymous download of native client updates, outside /api/v1/ (#606).
        path("", include("skeleton.system.native_clients.urls")),
        path("platform/", include("skeleton.web.platform.urls")),
        path("invite/<uuid:organization_id>/<str:token>/", invitation_view, name="invitation"),
        # The acceptance of an invitation to an organization's portal (#532).
        path(
            "portal/invite/<uuid:organization_id>/<str:token>/",
            external_invitation_view,
            name="external-invitation",
        ),
    ]
    if project_urls:
        routes.append(path("", include(project_urls)))
    routes.append(path("", include("skeleton.notifications.urls")))
    routes.append(path("", include("skeleton.files.urls")))
    routes.append(path("", include("skeleton.billing.urls")))
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
