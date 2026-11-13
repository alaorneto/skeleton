"""Navigation context for the SSR shell: organization switcher and access sections.

Only presentation data is produced here. Every target view keeps enforcing its own
authorization; a link shown here never grants access.
"""

from typing import Any

from django.http import HttpRequest
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy

from skeleton.tenancy.context import current_context, memberships_for_user

__all__ = ["ACCESS_SECTIONS", "organization_shell"]

# Administration of an organization grouped by what a person is trying to do.
# Each section is one sidebar entry; its resources become tabs on the list screens.
ACCESS_SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "people",
        gettext_lazy("Pessoas"),
        (
            ("memberships", gettext_lazy("Membros")),
            ("invitations", gettext_lazy("Convites")),
            ("member-roles", gettext_lazy("Papéis de membros")),
        ),
    ),
    (
        "groups",
        gettext_lazy("Grupos"),
        (
            ("groups", gettext_lazy("Grupos")),
            ("group-members", gettext_lazy("Membros de grupos")),
            ("group-roles", gettext_lazy("Papéis de grupos")),
        ),
    ),
    (
        "roles",
        gettext_lazy("Papéis e permissões"),
        (
            ("roles", gettext_lazy("Papéis")),
            ("table-permissions", gettext_lazy("Permissões de tabelas")),
            ("record-grants", gettext_lazy("Concessões de registros")),
        ),
    ),
)


def _access_navigation(organization_id: Any, resource: str | None, capabilities: frozenset[str]) -> dict[str, Any]:
    from skeleton.access.capabilities import RESOURCE_VIEW_CAPABILITIES

    sections = []
    current = None
    for section_id, label, resources in ACCESS_SECTIONS:
        tabs = [
            {
                "resource": name,
                "label": tab_label,
                "url": reverse("web:access-list", kwargs={"organization_id": organization_id, "resource": name}),
                "current": name == resource,
            }
            for name, tab_label in resources
            if RESOURCE_VIEW_CAPABILITIES[name] in capabilities
        ]
        if not tabs:
            continue  # A delegated manager sees only the sections of their capabilities.
        active = any(tab["current"] for tab in tabs)
        entry = {"id": section_id, "label": label, "url": tabs[0]["url"], "tabs": tabs, "current": active}
        if active:
            current = entry
        sections.append(entry)
    return {"access_sections": sections, "access_section": current}


def organization_shell(request: HttpRequest) -> dict[str, Any]:
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    memberships = memberships_for_user(user)
    from skeleton.flows.inbox import open_task_count
    from skeleton.notifications.inbox import counter_label, unread_count

    organization_ids = [item["organization_id"] for item in memberships]
    unread = unread_count(user, organization_ids=organization_ids)
    tasks = open_task_count(user, organization_ids=organization_ids)  # Own tasks of every organization (#409).
    values: dict[str, Any] = {
        "organization_memberships": memberships,
        "notifications_unread": unread,
        "notifications_label": counter_label(unread),
        "flow_tasks_open": tasks,
        "flow_tasks_label": counter_label(tasks),
    }
    organization_id = current_context().organization_id
    if organization_id is None:
        return values
    values["current_organization"] = next(
        (item for item in memberships if item["organization_id"] == organization_id),
        {"organization_id": organization_id, "organization_name": None},
    )
    match = getattr(request, "resolver_match", None)
    resource = match.kwargs.get("resource") if match is not None else None
    from skeleton.access.capabilities import capabilities_for

    capabilities = capabilities_for(user)
    values.update(_access_navigation(organization_id, resource, capabilities))
    values["admission_links"] = _admission_links(organization_id, capabilities, request.path)
    values["organization_brand"] = _brand(organization_id)
    return values


def _admission_links(organization_id: Any, capabilities: frozenset[str], path: str) -> list[dict[str, Any]]:
    """Join requests (#26) for deciders and the admission policy for who configures it."""
    links = []
    for capability, name, label in (
        ("admission.decide", "web:join-requests", gettext("Solicitações")),
        ("invitations.manage", "web:join-links", gettext("Links de ingresso")),
        ("admission.configure", "web:admission", gettext("Admissão")),
        ("admission.configure", "web:verified-domains", gettext("Domínios verificados")),
        ("identity.configure", "web:identity", gettext("Identidade")),
    ):
        if capability in capabilities:
            url = reverse(name, kwargs={"organization_id": organization_id})
            links.append({"label": label, "url": url, "current": path == url})
    return links


def _brand(organization_id: Any) -> dict[str, Any]:
    """Presentation of the current organization's brand (#168); CSS only from validated hex."""
    from skeleton.tenancy.branding import palette
    from skeleton.tenancy.organization_settings import current_settings

    record = current_settings()
    if record is None:
        return {}
    tokens = palette(record.brand_color)
    return {
        "display_name": record.display_name,
        "css": tokens.css() if tokens else "",
        "logo_url": reverse("web:organization-logo", kwargs={"organization_id": organization_id})
        if record.logo_key
        else "",
    }
