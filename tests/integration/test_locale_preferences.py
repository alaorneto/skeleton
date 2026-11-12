"""Language and time zone precedence on every surface (#170): profile > organization > instance."""

import pytest
from django.http import JsonResponse
from django.test import Client, override_settings
from django.urls import include, path, reverse
from django.utils import timezone, translation

from skeleton.service_api.identity import IdentityService
from skeleton.service_api.organizations import OrganizationService
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")
TWO_LANGUAGES = [("pt-br", "Português"), ("en", "English")]


def probe(request, organization_id=None):
    return JsonResponse({"language": translation.get_language(), "timezone": timezone.get_current_timezone_name()})


urlpatterns = [
    path("o/<uuid:organization_id>/locale-probe/", probe),
    path("api/v1/o/<uuid:organization_id>/locale-probe/", probe),
    path("locale-probe/", probe),
    path("", include("config.urls")),
]


@pytest.fixture
def scenario():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="locale")


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def set_profile(user, *, language="", zone=""):
    from skeleton.accounts.models import UserProfile

    with system_context(reason="test:locale-profile"):
        profile = UserProfile.objects.get(user=user)
        profile.language, profile.timezone = language, zone
        profile.save(update_fields={"language", "timezone"})


def set_organization(admin, organization, *, language="", zone=""):
    service = OrganizationService()
    with acting_as(admin, organization) as actor:
        revision = service.settings(actor=actor).revision
        service.update_settings(
            actor=actor,
            display_name="",
            default_language=language,
            default_timezone=zone,
            brand_color="",
            expected_revision=revision,
        )


def seen(client, organization=None, *, api=False):
    if organization is None:
        url = "/locale-probe/"
    else:
        url = f"/{'api/v1/' if api else ''}o/{organization.pk}/locale-probe/"
    response = client.get(url)
    assert response.status_code == 200
    return response.json()["language"], response.json()["timezone"], response["Content-Language"]


# The probe under /api/v1/ is a test-only route, outside the published OpenAPI contract.
@pytest.mark.no_api_contract
@override_settings(ROOT_URLCONF=__name__, LANGUAGES=TWO_LANGUAGES, SKELETON_DEFAULT_TIMEZONE="UTC")
def test_blank_profile_follows_the_organization_then_the_instance(scenario):
    set_profile(scenario.member)
    set_organization(scenario.admin_a, scenario.a, language="en", zone="America/Sao_Paulo")
    client = login(scenario.member)
    assert seen(client, scenario.a) == ("en", "America/Sao_Paulo", "en")
    assert seen(client, scenario.a, api=True) == ("en", "America/Sao_Paulo", "en")
    assert seen(client, scenario.b) == ("pt-br", "UTC", "pt-br")  # B has no defaults.
    assert seen(client) == ("pt-br", "UTC", "pt-br")  # Outside an organization: the instance.


@override_settings(ROOT_URLCONF=__name__, LANGUAGES=TWO_LANGUAGES)
def test_explicit_profile_wins_over_the_organization(scenario):
    set_profile(scenario.member, language="pt-br", zone="Asia/Tokyo")
    set_organization(scenario.admin_a, scenario.a, language="en", zone="America/Sao_Paulo")
    client = login(scenario.member)
    assert seen(client, scenario.a) == ("pt-br", "Asia/Tokyo", "pt-br")
    assert seen(client) == ("pt-br", "Asia/Tokyo", "pt-br")


@override_settings(
    ROOT_URLCONF=__name__,
    LANGUAGES=TWO_LANGUAGES,
    SKELETON_DEFAULT_LANGUAGE="en",
    SKELETON_DEFAULT_TIMEZONE="Europe/Lisbon",
)
def test_instance_defaults_and_browser_language_before_login():
    assert seen(Client()) == ("en", "Europe/Lisbon", "en")
    response = Client().get("/locale-probe/", HTTP_ACCEPT_LANGUAGE="pt-BR,pt;q=0.9")
    assert response.json()["language"] == "pt-br"
    assert "Accept-Language" in response["Vary"]
    assert Client().get("/locale-probe/", HTTP_ACCEPT_LANGUAGE="de").json()["language"] == "en"


@override_settings(ROOT_URLCONF=__name__, LANGUAGES=TWO_LANGUAGES)
def test_the_organization_comes_from_the_context_not_from_the_client(scenario):
    set_profile(scenario.member)
    set_organization(scenario.admin_b, scenario.b, language="en", zone="Asia/Tokyo")
    client = login(scenario.outsider)
    # Not a member of B: the tenant middleware answers 404 before any preference is read.
    assert client.get(f"/o/{scenario.b.pk}/locale-probe/").status_code == 404
    member = login(scenario.member)
    response = member.get("/locale-probe/", HTTP_X_ORGANIZATION=str(scenario.b.pk))
    assert response.json() == {"language": "pt-br", "timezone": "UTC"}


@override_settings(LANGUAGES=TWO_LANGUAGES)
def test_identity_service_reports_choices_and_effective_values(scenario):
    set_profile(scenario.member)
    set_organization(scenario.admin_a, scenario.a, language="en", zone="America/Sao_Paulo")
    with acting_as(scenario.member, scenario.a) as actor:
        me = IdentityService().me(actor=actor)
    assert (me.language, me.timezone) == ("", "")
    assert (me.effective_language, me.effective_timezone) == ("en", "America/Sao_Paulo")


def test_unsupported_language_is_refused_everywhere(scenario):
    from skeleton.service_api.errors import InvalidInput

    with acting_as(scenario.member, scenario.a) as actor:
        revision = IdentityService().me(actor=actor).revision
        with pytest.raises(InvalidInput):
            IdentityService().update_my_preferences(actor=actor, expected_revision=revision, language="en")
    with acting_as(scenario.admin_a, scenario.a) as admin, pytest.raises(InvalidInput):
        OrganizationService().update_settings(
            actor=admin,
            display_name="",
            default_language="en",
            default_timezone="",
            brand_color="",
            expected_revision=0,
        )


def test_html_lang_follows_the_active_language(scenario):
    client = login(scenario.member)
    page = client.get(reverse("web:dashboard", kwargs={"organization_id": scenario.a.pk})).content.decode()
    assert '<html lang="pt-br">' in page
    with override_settings(LANGUAGES=TWO_LANGUAGES):
        set_organization(scenario.admin_a, scenario.a, language="en")
        set_profile(scenario.member)
        page = client.get(reverse("web:dashboard", kwargs={"organization_id": scenario.a.pk})).content.decode()
    assert '<html lang="en">' in page
