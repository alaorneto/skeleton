"""Application administration is derived from the active, code-owned registry."""

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.urls import resolve, reverse

from skeleton.access.capabilities import APPS_MANAGE, require_capability
from skeleton.tenancy.context import current_context

from .app_registry import get_app
from .applications import require_application_enabled


def require_apps_manager(user):
    """The apps panel exists only for ``apps.manage`` (app manager or administrator): 404 otherwise (#24)."""
    from skeleton.access.capabilities import capabilities_for

    if APPS_MANAGE not in capabilities_for(user):
        raise Http404


def application_manifest(app_label):
    try:
        return get_app(app_label)
    except KeyError:
        raise Http404 from None


def application_identity(manifest):
    """Never derive release identity from editable catalog descriptions."""
    return {
        "app_id": manifest.app_id or manifest.app_label,
        "app_label": manifest.app_label,
        "label": manifest.label,
        "description": manifest.description,
        "version": manifest.version,
        "origin": manifest.origin,
        "compatibility": (manifest.canonical or {}).get("compatibility", {}),
        "dependencies": manifest.dependencies,
    }


def resolve_application_screen(app_label, screen_id, *, user, scope):
    """Shared policy for discovery, dispatch, and app-owned direct URLs."""
    manifest = application_manifest(app_label)
    screen = next((item for item in manifest.administration.screens if item.id == screen_id), None)
    if screen is None or screen.scope != scope:
        raise Http404
    if scope == "platform":
        from skeleton.accounts.platform import require_platform_context

        require_platform_context(user)
    else:
        # Tenant parameter screens (organization_admin_only in the manifest) need apps.manage (#24).
        require_capability(user, APPS_MANAGE)
        if screen.kind == "custom":
            from skeleton.common.exceptions import ApplicationUnavailable

            from .availability import app_available

            # App-owned code never runs for an organization the platform did not admit (#253); the
            # core configuration screen stays reachable so the organization can disable the app.
            if not app_available(app_label):
                raise ApplicationUnavailable("Application is unavailable to this organization.")
    return manifest, screen


def screen_sections(manifest, *, user, scope):
    entries = {}
    for screen in sorted(manifest.administration.screens, key=lambda item: (item.order, item.id)):
        if screen.scope != scope:
            continue
        try:
            resolve_application_screen(manifest.app_label, screen.id, user=user, scope=scope)
        except PermissionDenied:
            continue
        url = (
            reverse("platform:app-screen", kwargs={"app_label": manifest.app_label, "screen_id": screen.id})
            if scope == "platform"
            else reverse(
                "application-admin-screen",
                kwargs={
                    "organization_id": current_context().organization_id,
                    "app_label": manifest.app_label,
                    "screen_id": screen.id,
                },
            )
        )
        entries.setdefault(screen.section_id, []).append({"id": screen.id, "label": screen.label, "url": url})
    return [
        {"id": section.id, "label": section.label, "screens": entries[section.id]}
        for section in sorted(manifest.administration.sections, key=lambda item: (item.order, item.id))
        if section.id in entries
    ]


def guard_application_screen(request, view_name, *, app_label=None):
    """Parameter screens require tenant administration; other app routes require activation."""
    if app_label is not None:
        manifest = application_manifest(app_label)
    else:
        namespace = view_name.split(":", 1)[0]
        try:
            manifest = get_app(namespace)
        except KeyError:
            return
    for screen in manifest.administration.screens:
        if screen.scope == "organization" and screen.kind == "custom" and screen.url_name == view_name:
            resolve_application_screen(manifest.app_label, screen.id, user=request.user, scope="organization")
            return
    require_application_enabled(manifest.app_label)


def tenant_screen_url(manifest, screen, organization_id):
    if screen.kind == "configuration":
        return reverse(
            "application-configuration", kwargs={"organization_id": organization_id, "app_label": manifest.app_label}
        )
    if not screen.url_name or screen.url_name.split(":", 1)[0] != manifest.app_label:
        raise Http404
    url = reverse(screen.url_name, kwargs={"organization_id": organization_id})
    prefix = f"/o/{organization_id}/apps/{manifest.app_label}/"
    if not url.startswith(prefix) or url.startswith("//") or resolve(url).view_name != screen.url_name:
        raise Http404
    return url
