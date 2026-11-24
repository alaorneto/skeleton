"""One icon per concept, and one concept per icon (#768).

Templates name a concept (``{% include ".../icon.html" with name="billing"|concept_icon %}``),
never the icon of a concept directly, so the same concept has the same icon in the header, the
sidebars, the settings hub and the empty states, and two concepts never share an icon. Actions
(create, edit, delete…), feedback (success, warning…) and interface controls (chevrons, menu,
color scheme…) have their own icons, which are never the icon of a concept.

``tests/unit/test_concept_icons.py`` checks that the map is one-to-one, that the two sets do not
meet, that every icon exists in ``components/icon.html`` and that templates only write action
icons literally.
"""

from __future__ import annotations

CONCEPT_ICONS: dict[str, str] = {
    # Shell and areas.
    "home": "home",
    "overview": "grid",
    "organization": "building",
    "configuration": "cog",
    "instance_configuration": "settings",
    "platform": "server",
    "account": "user",
    "notifications": "bell",
    "tasks": "clipboard-check",
    # People and access.
    "people": "users",
    "groups": "user-group",
    "roles": "key",
    "external_people": "user-plus",
    "security": "shield",
    "password": "lock",
    "email": "envelope",
    "api_tokens": "ticket",
    "connected_devices": "signal",
    "identity": "finger-print",
    "privacy": "user-minus",
    # Admission.
    "join_requests": "inbox",
    "join_links": "link",
    "admission": "badge-check",
    "email_domains": "at-symbol",
    # Organization pages and addresses.
    "site_domains": "globe",
    "public_page": "window",
    # Apps and integrations.
    "apps": "cube",
    "webhooks": "bolt",
    "service_accounts": "cpu-chip",
    "flows": "share",
    "calendar": "calendar",
    "documentation": "book-open",
    # Data.
    "tables": "table",
    "audit": "document-search",
    "files": "document",
    "transfers": "arrow-down-tray",
    # Operation of the platform.
    "billing": "credit-card",
    "appearance": "paint-brush",
    "jobs": "queue-list",
    "updates": "arrow-up-circle",
    "native_clients": "device-phone",
    "api_clients": "command-line",
    "remote_instances": "server-stack",
    "bundles": "rectangle-stack",
    "storage": "database",
    "insights": "chart-bar",
}

# Icons written literally in templates: actions, feedback and interface controls.
ACTION_ICONS: frozenset[str] = frozenset(
    {
        # Actions.
        "plus",
        "pencil",
        "trash",
        "restore",
        "refresh",
        "archive",
        "copy",
        "eye",
        "arrow-up-tray",
        "external",
        "search",
        "filter",
        "login",
        "logout",
        # Navigation and controls.
        "arrow-left",
        "arrow-right",
        "chevron-down",
        "chevron-left",
        "chevron-right",
        "menu",
        "close",
        "check",
        "sun",
        "moon",
        "computer",
        # Feedback and time.
        "info",
        "check-circle",
        "warning",
        "error",
        "clock",
        # The product mark.
        "logo",
    }
)


def concept_icon(concept: str) -> str:
    """The icon of a concept; an unknown concept falls back to the neutral dot of icon.html."""
    return CONCEPT_ICONS.get(str(concept), "")
