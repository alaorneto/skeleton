"""Navigation context for the SSR shell: organization switcher and access sections.

Only presentation data is produced here. Every target view keeps enforcing its own
authorization; a link shown here never grants access.
"""

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy

from skeleton.tenancy.context import current_context, memberships_for_user

__all__ = [
    "ACCESS_SECTIONS",
    "SETTINGS_GROUPS",
    "SETTINGS_ROUTE_NAMES",
    "SettingsItem",
    "can_open_settings",
    "organization_shell",
    "settings_navigation",
]

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


@dataclass(frozen=True)
class SettingsItem:
    """One entry of the organization settings area (#621): presentation data, never a permission.

    ``gate`` decides whether the entry is shown: a capability name, ``organization_admin``,
    ``custom_domains`` (the feature on and the administrator), ``billing`` (billing on and
    ``billing.view``), ``audit`` (the administrator or the ``audit`` action on some table),
    ``member`` (any member) or ``access:<section>`` (a section of ``ACCESS_SECTIONS`` the person
    sees). ``routes`` are the view names that mark the entry as the current one.
    """

    url_name: str
    label: Any
    icon: str
    gate: str
    routes: tuple[str, ...] = ()


# The settings area of an organization, grouped by what a person is trying to do (#621). Only the
# links move here: every target view keeps its own authorization.
SETTINGS_GROUPS: tuple[tuple[str, Any, tuple[SettingsItem, ...]], ...] = (
    (
        "organization",
        gettext_lazy("Organização"),
        (
            SettingsItem(
                "web:organization-settings",
                gettext_lazy("Geral"),
                "building",
                "organization_admin",
                ("web:organization-settings", "web:organization-logo-update", "web:organization-icon-update"),
            ),
            SettingsItem(
                "web:site-domains",
                gettext_lazy("Domínios próprios"),
                "globe",
                "custom_domains",
                ("web:site-domains", "web:site-domain-action"),
            ),
        ),
    ),
    (
        "people",
        gettext_lazy("Pessoas e acesso"),
        (
            SettingsItem("web:access-list", gettext_lazy("Pessoas"), "users", "access:people"),
            SettingsItem("web:access-list", gettext_lazy("Grupos"), "user-group", "access:groups"),
            SettingsItem("web:access-list", gettext_lazy("Papéis e permissões"), "key", "access:roles"),
            SettingsItem(
                "web:external",
                gettext_lazy("Pessoas externas"),
                "users",
                "external.manage",
                ("web:external", "web:external-access-revoke", "web:external-invitation-revoke"),
            ),
        ),
    ),
    (
        "admission",
        gettext_lazy("Admissão"),
        (
            SettingsItem(
                "web:join-requests",
                gettext_lazy("Solicitações"),
                "envelope",
                "admission.decide",
                ("web:join-requests", "web:join-request-decide"),
            ),
            SettingsItem(
                "web:join-links",
                gettext_lazy("Links de ingresso"),
                "link",
                "invitations.manage",
                ("web:join-links", "web:join-link-revoke"),
            ),
            SettingsItem("web:admission", gettext_lazy("Admissão"), "badge-check", "admission.configure"),
            SettingsItem(
                "web:verified-domains",
                gettext_lazy("Domínios verificados"),
                "globe",
                "admission.configure",
                ("web:verified-domains", "web:verified-domain-action"),
            ),
            SettingsItem(
                "web:identity",
                gettext_lazy("Identidade"),
                "shield",
                "identity.configure",
                ("web:identity", "web:identity-admission"),
            ),
        ),
    ),
    (
        "applications",
        gettext_lazy("Aplicativos e integrações"),
        (
            SettingsItem(
                "application-list",
                gettext_lazy("Aplicativos"),
                "cube",
                "apps.manage",
                ("application-list", "application-admin", "application-admin-screen", "application-configuration"),
            ),
            SettingsItem(
                "integrations:webhooks",
                gettext_lazy("Webhooks"),
                "link",
                "integrations.manage",
                (
                    "integrations:webhooks",
                    "integrations:webhook-create",
                    "integrations:webhook",
                    "integrations:webhook-rotate",
                    "integrations:webhook-delete",
                    "integrations:webhook-deliveries",
                    "integrations:webhook-delivery-resend",
                    "integrations:webhook-ping",
                ),
            ),
            SettingsItem(
                "integrations:service-accounts",
                gettext_lazy("Contas de serviço"),
                "key",
                "integrations.manage",
                (
                    "integrations:service-accounts",
                    "integrations:service-account",
                    "integrations:service-account-deactivate",
                    "integrations:service-token-revoke",
                ),
            ),
            SettingsItem(
                "flows:operations",
                gettext_lazy("Fluxos"),
                "refresh",
                "flows.manage",
                ("flows:operations", "flows:drafts", "flows:draft"),
            ),
        ),
    ),
    (
        "data",
        gettext_lazy("Dados"),
        (
            SettingsItem("web:catalog", gettext_lazy("Tabelas"), "table", "member"),
            SettingsItem("web:audit", gettext_lazy("Auditoria"), "clock", "audit"),
        ),
    ),
    (
        "billing",
        gettext_lazy("Cobrança"),
        (
            SettingsItem(
                "billing-overview",
                gettext_lazy("Cobrança"),
                "document",
                "billing",
                ("billing-overview", "billing-action", "billing-pay"),
            ),
        ),
    ),
)
# Routes whose screens show the settings menu in the sidebar: the items above, their sub-routes
# and the hub. Record screens (web:records, web:record...) keep the functional menu: apps link there.
SETTINGS_ROUTE_NAMES: frozenset[str] = frozenset(
    {
        "web:organization-admin",
        "web:access-create",
        "web:access-edit",
        "web:access-delete",
        "web:invitation-resend",
        *(item.url_name for _, _, items in SETTINGS_GROUPS for item in items),
        *(route for _, _, items in SETTINGS_GROUPS for item in items for route in item.routes),
    }
)
# Entries every member sees: they alone never show the gear (#621).
EVERY_MEMBER = frozenset({"member"})


def _gate_open(gate: str, facts: dict[str, Any]) -> bool:
    if gate == "member":
        return True
    if gate == "organization_admin":
        return facts["is_admin"]
    if gate == "custom_domains":
        return facts["is_admin"] and facts["custom_domains"]
    if gate == "billing":
        return facts["billing"] and "billing.view" in facts["capabilities"]
    if gate == "audit":
        return facts["is_admin"] or any(action == "audit" for _, action in facts["table_permissions"])
    if gate.startswith("access:"):
        return gate.removeprefix("access:") in facts["access_sections"]
    return gate in facts["capabilities"]


def settings_navigation(
    organization_id: Any,
    *,
    view_name: str,
    capabilities: frozenset[str],
    is_admin: bool,
    table_permissions: frozenset[tuple[str, str]],
    access_sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The groups of the settings area with only the entries this person sees (no query of its own).

    Each entry is ``{label, icon, url, current, every_member}``; a group without entries is left out.
    """
    from skeleton.tenancy.site_domains import custom_domains_enabled

    sections = {section["id"]: section for section in access_sections}
    facts = {
        "capabilities": capabilities,
        "is_admin": is_admin,
        "table_permissions": table_permissions,
        "access_sections": sections,
        "custom_domains": custom_domains_enabled(),
        "billing": bool(getattr(settings, "SKELETON_BILLING_ENABLED", False)),
    }
    in_area = view_name in SETTINGS_ROUTE_NAMES
    groups = []
    for group_id, label, items in SETTINGS_GROUPS:
        entries = []
        for item in items:
            if not _gate_open(item.gate, facts):
                continue
            if item.gate.startswith("access:"):
                section = sections[item.gate.removeprefix("access:")]
                url, current = section["url"], bool(section["current"]) and in_area
            else:
                url = reverse(item.url_name, kwargs={"organization_id": organization_id})
                current = view_name in (item.routes or (item.url_name,))
            entries.append(
                {
                    "label": item.label,
                    "icon": item.icon,
                    "url": url,
                    "current": current,
                    "every_member": item.gate in EVERY_MEMBER,
                }
            )
        if entries:
            groups.append({"id": group_id, "label": label, "items": entries})
    return groups


def settings_navigation_for(request: HttpRequest, organization_id: Any) -> list[dict[str, Any]]:
    """``settings_navigation`` for the person of ``request`` in the context organization (the hub)."""
    user = request.user
    facts = _navigation_facts(request, user, organization_id)
    match = getattr(request, "resolver_match", None)
    resource = match.kwargs.get("resource") if match is not None else None
    access = _access_navigation(organization_id, resource, facts["capabilities"])
    return settings_navigation(
        organization_id,
        view_name=match.view_name if match is not None else "",
        capabilities=facts["capabilities"],
        is_admin=facts["is_admin"],
        table_permissions=facts["table_permissions"],
        access_sections=access["access_sections"],
    )


def can_open_settings(groups: list[dict[str, Any]]) -> bool:
    """Whether the gear shows: some entry besides the ones every member sees (Tables)."""
    return any(not item["every_member"] for group in groups for item in group["items"])


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
    from skeleton.appearance.snapshot import current_appearance

    from .instance_brand import instance_brand, is_safe_path

    site = getattr(request, "organization_site", None)  # Set by OrganizationSiteMiddleware (#482).
    # The brand of the instance (#598), under the organization's own brand on its screens.
    brand = instance_brand(current_appearance(), safe=is_safe_path(request.path))
    shell: dict[str, Any] = {
        "site_mode": site is not None,
        "organization_site": site,  # Not "site": allauth views pass their own "site" (RequestSite).
        "instance_brand": brand,
        # The suffix of every page title (#484): the organization on its own address, else the product.
        "site_name": site.name if site is not None else (brand["name"] or "Skeleton"),
        # The favicon (#485): the site's public icon on its host; inside /o/<org>/ the members' icon.
        "organization_icon_url": reverse("web:site-icon") + f"?v={site.icon_key[-12:]}"
        if site is not None and site.icon_key
        else "",
    }
    if site is not None and current_context().organization_id is None:
        # Before login or outside the tenant context: the brand the administrator published (#484).
        shell["organization_brand"] = _site_brand(site)
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return shell
    context = current_context()
    if context.external and context.organization_id is not None:
        # The portal (#533): only the organization's brand, never member navigation or counters.
        shell["organization_brand"] = _brand(context.organization_id, site)
        shell["portal_organization_id"] = context.organization_id
        return shell
    shell.update(_signed_in_shell(request, user, site))
    return shell


def _signed_in_shell(request: HttpRequest, user: Any, site: Any) -> dict[str, Any]:
    # On an organization host the memberships (and so the counters) are only that organization's (#483).
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
    view_name = match.view_name if match is not None else ""
    facts = _navigation_facts(request, user, organization_id)
    capabilities = facts["capabilities"]
    values.update(_access_navigation(organization_id, resource, capabilities))
    values["admission_links"] = _admission_links(organization_id, capabilities, request.path)
    # The settings area (#621): the gear for whom sees some entry, and the settings menu on its routes.
    groups = settings_navigation(
        organization_id,
        view_name=view_name,
        capabilities=capabilities,
        is_admin=facts["is_admin"],
        table_permissions=facts["table_permissions"],
        access_sections=values["access_sections"],
    )
    values["settings_navigation"] = groups
    values["can_open_settings"] = can_open_settings(groups)
    values["settings_area"] = view_name in SETTINGS_ROUTE_NAMES
    values["organization_brand"] = _brand(organization_id, site)
    from skeleton.records.search import global_search_declared

    if global_search_declared():  # No query: only says whether any registered table opted in.
        values["global_search_url"] = reverse("web:search", kwargs={"organization_id": organization_id})
    if site is None:
        icon = values["organization_brand"].get("icon_key", "")
        if icon:
            values["organization_icon_url"] = (
                reverse("web:organization-icon", kwargs={"organization_id": organization_id}) + f"?v={icon[-12:]}"
            )
    return values


def _navigation_facts(request: HttpRequest, user: Any, organization_id: Any) -> dict[str, Any]:
    """Capabilities, administrator and table permissions of the person in this organization.

    The processor ``skeleton.system.applications.application_navigation`` runs first in the same
    render and leaves what it read on the request (``skeleton_navigation_facts``), for the same
    person and organization: the shell reuses it instead of asking the database again.
    """
    facts = getattr(request, "skeleton_navigation_facts", None)
    if facts is not None and facts.get("user_id") == user.pk and facts.get("organization_id") == organization_id:
        return facts
    from skeleton.access.capabilities import capabilities_for
    from skeleton.access.policies import is_organization_admin, table_permissions

    return {
        "capabilities": capabilities_for(user),
        "is_admin": is_organization_admin(user),
        "table_permissions": table_permissions(user),
    }


def _admission_links(organization_id: Any, capabilities: frozenset[str], path: str) -> list[dict[str, Any]]:
    """Join requests (#26) for deciders and the admission policy for who configures it."""
    links = []
    for capability, name, label in (
        ("admission.decide", "web:join-requests", gettext("Solicitações")),
        ("invitations.manage", "web:join-links", gettext("Links de ingresso")),
        ("admission.configure", "web:admission", gettext("Admissão")),
        ("admission.configure", "web:verified-domains", gettext("Domínios verificados")),
        ("identity.configure", "web:identity", gettext("Identidade")),
        ("external.manage", "web:external", gettext("Pessoas externas")),
    ):
        if capability in capabilities:
            url = reverse(name, kwargs={"organization_id": organization_id})
            links.append({"label": label, "url": url, "current": path == url})
    return links


def _brand(organization_id: Any, site: Any = None) -> dict[str, Any]:
    """Presentation of the current organization's brand (#168); CSS only from validated hex."""
    from skeleton.tenancy.branding import palette
    from skeleton.tenancy.organization_settings import current_settings

    record = current_settings()
    if record is None:
        return {}
    tokens = palette(record.brand_color)
    logo_url = ""
    if record.logo_key:
        if site is not None and site.organization_id == organization_id and site.logo_key == record.logo_key:
            logo_url = _site_logo_url(site)  # One public, cacheable URL on the organization's own host (#484).
        else:
            logo_url = reverse("web:organization-logo", kwargs={"organization_id": organization_id})
    return {
        "display_name": record.display_name,
        "css": tokens.css() if tokens else "",
        "color": tokens.brand if tokens else "",
        "logo_url": logo_url,
        "icon_key": record.icon_key,
    }


def _site_logo_url(site: Any) -> str:
    return reverse("web:site-logo") + f"?v={site.logo_key[-12:]}"


def _site_brand(site: Any) -> dict[str, Any]:
    """The published brand of the organization host, read from the site index (no tenant context needed)."""
    from skeleton.tenancy.branding import palette

    tokens = palette(site.brand_color)
    return {
        "display_name": site.name,
        "css": tokens.css() if tokens else "",
        "color": tokens.brand if tokens else "",
        "logo_url": _site_logo_url(site) if site.logo_key else "",
    }
