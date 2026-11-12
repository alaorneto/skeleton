"""Every visible text of the key pages goes through gettext (#174).

A pseudo translation marks each translated message as ``⟦…⟧``. The key pages are rendered for an
organization administrator, a platform administrator and an anonymous visitor; anything visible
left outside the marks, apart from the data of the fixture (names, emails, codes, numbers), is a
string that escaped gettext.
"""

import html
import re

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")
OPEN, CLOSE = "⟦", "⟧"
PLATFORM_PASSWORD = "Platform-i18n-coverage-2026!913"
# Product names and raw codes shown as data (the origin of the core, for example).
ALWAYS_ALLOWED = {
    "source",
    "read",
    "delete",
    "audit",
    "manage_access",
    "project-local",
    "create",
    "update",
    "restore",
    "wheel",
    "Skeleton",
    "Flowbite",
    "Tailwind",
    "Inter",
    "MFA",
    "API",
    "URL",
    "HTTPS",
    "ID",
    "UUID",
    "CSV",
    "JSON",
}


@pytest.fixture
def pseudo(monkeypatch):
    from django.utils import translation

    real = translation._trans

    def mark(message, *args):
        return f"{OPEN}{message}{CLOSE}"

    real.gettext  # noqa: B018 - load the real implementation before patching it.
    monkeypatch.setattr(real, "gettext", lambda message: mark(message))
    monkeypatch.setattr(real, "pgettext", lambda context, message: mark(message))
    monkeypatch.setattr(real, "ngettext", lambda singular, plural, number: mark(singular if number == 1 else plural))
    monkeypatch.setattr(
        real, "npgettext", lambda context, singular, plural, number: mark(singular if number == 1 else plural)
    )


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="i18n-coverage")


def login(email, password):
    client = Client()
    assert client.post(reverse("account_login"), {"login": email, "password": password}).status_code == 302
    return client


def visible_text(page: str) -> list[str]:
    # Scripts, styles and data blocks (code, the JSON of audit changes) are not interface text.
    page = re.sub(r"<(script|style|code|pre)\b.*?</\1>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    attributes = re.findall(r'\b(?:alt|title|placeholder|aria-label)="([^"]*)"', page)
    text = re.sub(r"<[^>]+>", "\n", page)
    return [html.unescape(chunk) for chunk in [*text.split("\n"), *attributes]]


def leftovers(page: str, allowed: set[str]) -> dict[str, str]:
    """Words outside the marks, each with the unmarked text it was found in."""
    found: dict[str, str] = {}
    for chunk in visible_text(page):
        previous = None
        while previous != chunk:  # Innermost first: a mark may contain another.
            previous, chunk = chunk, re.sub(rf"{OPEN}[^{OPEN}{CLOSE}]*{CLOSE}", " ", chunk)
        for token in chunk.split():
            if re.search(r"[0-9_./@:]", token):
                continue  # Identifiers, model labels, dates, emails, URLs.
            word = token.strip(".,;:!?()[]{}\"'«»“”‘’·—–-")
            if len(word) < 3 or not re.fullmatch(r"[A-Za-zÀ-ÿ'’-]+", word):
                continue
            if word not in allowed and word.lower() not in allowed:
                found.setdefault(word, " ".join(chunk.split())[:160])
    return found


def fixture_words(scenario) -> set[str]:
    from zoneinfo import available_timezones

    from skeleton.accounts.models import User
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Organization

    words = set(ALWAYS_ALLOWED)
    # IANA zone names are data in the time zone selects.
    for zone in available_timezones():
        words |= {zone, *re.split(r"[/_]", zone), *re.findall(r"[A-Za-z]+", zone)}
    # Language names are shown in their own language (endonyms), never translated.
    from django.conf import settings

    words |= {name for _code, name in settings.LANGUAGES}
    with system_context(reason="test:i18n-coverage"):
        values = [*Organization.all_objects.values_list("name", "slug")]
        values += [*User.all_objects.values_list("email", "first_name", "last_name")]
    # Role and group names are data the organization types (builtin names included).
    from skeleton.access.models import AccessGroup, Role

    for organization in (scenario.a, scenario.b):
        with system_context(reason="test:i18n-coverage-org", organization_id=organization.pk):
            values += [(name,) for name in Role.objects.values_list("name", flat=True)]
            values += [(name, code) for name, code in AccessGroup.objects.values_list("name", "code")]
            values += [(code,) for code in Role.objects.values_list("code", flat=True)]
    from skeleton.system.app_registry import all_apps

    # App manifests are data of each app (name, description, menu labels), not core text.
    for manifest in all_apps():
        values.append(
            (manifest.label, getattr(manifest, "description", ""), *(entry.label for entry in manifest.navigation))
        )
    for row in values:
        for value in row:
            words |= set(re.findall(r"[A-Za-zÀ-ÿ'’-]+", value or ""))
            words |= {part.lower() for part in re.findall(r"[A-Za-zÀ-ÿ]+", value or "")}
    return words


def assert_translated(client, url, allowed):
    response = client.get(url, follow=True)
    assert response.status_code == 200, url
    page = response.content.decode()
    missing = leftovers(page, allowed)
    assert not missing, f"{url}: text outside gettext: {missing}"


@pytest.mark.needs_example_app
def test_organization_pages_are_translated(scenario, pseudo):

    allowed = fixture_words(scenario)
    organization = scenario.a.pk
    client = login(scenario.admin_a.email, FIXTURE_PASSWORD)
    pages = [
        reverse("web:dashboard", kwargs={"organization_id": organization}),
        reverse("web:catalog", kwargs={"organization_id": organization}),
        reverse("web:audit", kwargs={"organization_id": organization}),
        reverse("web:organization-settings", kwargs={"organization_id": organization}),
        reverse("application-list", kwargs={"organization_id": organization}),
        reverse("integrations:webhooks", kwargs={"organization_id": organization}),
        reverse("integrations:service-accounts", kwargs={"organization_id": organization}),
        reverse("web:profile"),
        reverse("web:home"),
    ]
    for resource in ("memberships", "invitations", "roles", "groups", "table-permissions"):
        pages.append(reverse("web:access-list", kwargs={"organization_id": organization, "resource": resource}))
    for url in pages:
        assert_translated(client, url, allowed)


@pytest.mark.needs_example_app
def test_platform_pages_are_translated(scenario, pseudo):
    from skeleton.accounts.services import bootstrap_identity

    admin, _ = bootstrap_identity(
        email="platform-i18n@example.test", organization="platform-i18n", name="Plataforma", password=PLATFORM_PASSWORD
    )
    allowed = fixture_words(scenario)
    client = login(admin.email, PLATFORM_PASSWORD)
    for name in (
        "platform:overview",
        "platform:organizations",
        "platform:users",
        "platform:audit",
        "platform:jobs",
        "platform:apps",
    ):
        assert_translated(client, reverse(name), allowed)


@pytest.mark.needs_example_app
def test_anonymous_pages_are_translated(scenario, pseudo):
    allowed = fixture_words(scenario)
    client = Client()
    for name in ("account_login", "account_reset_password"):
        assert_translated(client, reverse(name), allowed)


def test_a_fixed_text_is_caught():
    page = f"<p>{OPEN}Salvar{CLOSE}</p><p>Texto esquecido</p><img alt='x' title=\"Sem tradução\">"
    assert set(leftovers(page, set())) == {"Texto", "esquecido", "Sem", "tradução"}
    assert leftovers(f"<p>{OPEN}Valor {OPEN}dentro{CLOSE} fim{CLOSE}</p>", set()) == {}
