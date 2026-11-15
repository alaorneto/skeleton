"""DNS label rules shared by the setup, the platform forms and the organization sites (#478).

Pure rules: no other ``skeleton.*`` package and no model is imported here (layering rule R1).
"""

import re

LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Names an organization may never take: they would pass for a part of the platform.
RESERVED_LABELS = frozenset(
    {
        "www",
        "api",
        "app",
        "apps",
        "admin",
        "platform",
        "plataforma",
        "static",
        "assets",
        "cdn",
        "media",
        "mail",
        "email",
        "smtp",
        "imap",
        "pop",
        "ftp",
        "ns",
        "ns1",
        "ns2",
        "mx",
        "status",
        "health",
        "setup",
        "accounts",
        "account",
        "login",
        "logout",
        "auth",
        "sso",
        "oidc",
        "saml",
        "docs",
        "help",
        "ajuda",
        "support",
        "suporte",
        "billing",
        "cobranca",
        "test",
        "teste",
        "staging",
        "dev",
        "demo",
        "localhost",
        "skeleton",
        "o",
        "me",
        "join",
        "invite",
        "site",
        "profile",
    }
)


def slug_problem(value: str) -> str | None:
    """``None`` when ``value`` can be a host label; otherwise ``"format"`` or ``"reserved"``."""
    if not isinstance(value, str) or not LABEL.fullmatch(value) or value.startswith("xn--"):
        return "format"
    return "reserved" if value in RESERVED_LABELS else None
