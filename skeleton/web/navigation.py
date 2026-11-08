"""Navigation context for the SSR shell: organization switcher and access sections.

Only presentation data is produced here. Every target view keeps enforcing its own
authorization; a link shown here never grants access.
"""

from typing import Any

from django.http import HttpRequest
from django.urls import reverse

from skeleton.tenancy.context import current_context, memberships_for_user

__all__ = ["ACCESS_SECTIONS", "organization_shell"]

# Administration of an organization grouped by what a person is trying to do.
# Each section is one sidebar entry; its resources become tabs on the list screens.
ACCESS_SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "people",
        "Pessoas",
        (("memberships", "Membros"), ("invitations", "Convites"), ("member-roles", "Papéis de membros")),
    ),
    (
        "groups",
        "Grupos",
        (("groups", "Grupos"), ("group-members", "Membros de grupos"), ("group-roles", "Papéis de grupos")),
    ),
    (
        "roles",
        "Papéis e permissões",
        (
            ("roles", "Papéis"),
            ("table-permissions", "Permissões de tabelas"),
            ("record-grants", "Concessões de registros"),
        ),
    ),
)


def _access_navigation(organization_id: Any, resource: str | None) -> dict[str, Any]:
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
        ]
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
    values: dict[str, Any] = {"organization_memberships": memberships}
    organization_id = current_context().organization_id
    if organization_id is None:
        return values
    values["current_organization"] = next(
        (item for item in memberships if item["organization_id"] == organization_id),
        {"organization_id": organization_id, "organization_name": None},
    )
    match = getattr(request, "resolver_match", None)
    resource = match.kwargs.get("resource") if match is not None else None
    values.update(_access_navigation(organization_id, resource))
    return values
