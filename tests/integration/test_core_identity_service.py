"""IdentityService of the core service API (#102)."""

import dataclasses

import pytest
from django.test import override_settings

from skeleton.service_api.errors import Conflict, ContextRequired, InvalidInput, NotFound
from skeleton.service_api.identity import IdentityService, PersonSummary, UserProfileInfo
from skeleton.tenancy.context import request_context
from skeleton.testing.service_api import (
    acting_as,
    assert_cas_conflict,
    assert_rejects_other_actor,
    assert_service_contract,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
FORBIDDEN_FIELDS = {"password", "auth_version", "is_superuser", "is_staff", "last_login", "mfa", "authenticators"}


def test_contract():
    assert_service_contract(IdentityService)
    for dto in (UserProfileInfo, PersonSummary):
        assert not FORBIDDEN_FIELDS & {field.name for field in dataclasses.fields(dto)}


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="identity")


@pytest.mark.needs_example_app
def test_me_works_with_and_without_organization(scenario):
    with acting_as(scenario.member, scenario.a) as actor:
        me = IdentityService().me(actor=actor)
    assert (me.sysid, me.email, me.language, me.kind) == (scenario.member.pk, scenario.member.email, "", "person")
    assert me.effective_language == "pt-br"  # A new profile follows the defaults (#170).
    with request_context(actor=scenario.member, request_id=None, source="test:identity-global"):
        assert IdentityService().me(actor=scenario.member).sysid == scenario.member.pk
    assert_rejects_other_actor(scenario.caller(lambda actor: IdentityService().me(actor=scenario.denied)))


@pytest.mark.needs_example_app
def test_get_person_is_limited_to_active_members_and_email_needs_members_view(scenario):
    service = IdentityService()
    with acting_as(scenario.member, scenario.a) as actor:
        person = service.get_person(actor=actor, user_id=scenario.denied.pk)
        assert (person.sysid, person.email) == (scenario.denied.pk, None)
        for foreign in (scenario.admin_b.pk, scenario.outsider.pk):
            with pytest.raises(NotFound):
                service.get_person(actor=actor, user_id=foreign)
        with pytest.raises(InvalidInput):
            service.get_person(actor=actor, user_id="not-a-uuid")
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert service.get_person(actor=admin, user_id=scenario.denied.pk).email == scenario.denied.email
    with request_context(actor=scenario.member, request_id=None, source="test:identity-no-tenant"):
        with pytest.raises(ContextRequired):
            service.get_person(actor=scenario.member, user_id=scenario.denied.pk)


@pytest.mark.needs_example_app
def test_inactive_membership_is_not_found(scenario):
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:identity-deactivate", organization_id=scenario.a.pk):
        membership = Membership.objects.get(user=scenario.denied)
        membership.is_active = False
        membership.save(update_fields={"is_active"})
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(NotFound):
        IdentityService().get_person(actor=actor, user_id=scenario.denied.pk)


@override_settings(LANGUAGES=[("pt-br", "Português"), ("en", "English")])
@pytest.mark.needs_example_app
def test_update_my_preferences_with_cas(scenario):
    service = IdentityService()
    with acting_as(scenario.member, scenario.a) as actor:
        revision = service.me(actor=actor).revision
    updated = scenario.caller(
        lambda actor: service.update_my_preferences(actor=actor, expected_revision=revision, language="en")
    )()
    assert (updated.language, updated.revision) == ("en", revision + 1)
    first = scenario.caller(
        lambda actor: service.update_my_preferences(
            actor=actor, expected_revision=revision + 1, timezone="America/Sao_Paulo"
        )
    )
    second = scenario.caller(
        lambda actor: service.update_my_preferences(actor=actor, expected_revision=revision + 1, timezone="UTC")
    )
    assert_cas_conflict(first, second)
    with acting_as(scenario.member, scenario.a) as actor:
        current = service.me(actor=actor).revision
        with pytest.raises(InvalidInput):
            service.update_my_preferences(actor=actor, expected_revision=current, language="xx")
        with pytest.raises(InvalidInput):
            service.update_my_preferences(actor=actor, expected_revision=current, timezone="Mars/Olympus")
        with pytest.raises(InvalidInput):
            service.update_my_preferences(actor=actor, expected_revision=current)
        with pytest.raises(Conflict):
            service.update_my_preferences(actor=actor, expected_revision=current - 1, language="pt-br")


@pytest.mark.needs_example_app
def test_get_person_reads_membership_user_and_profile_in_one_query(scenario):
    """No separate UserProfile query after the membership (#231)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with acting_as(scenario.member, scenario.a) as actor:
        with CaptureQueriesContext(connection) as captured:
            person = IdentityService().get_person(actor=actor, user_id=scenario.denied.pk)
    assert person.sysid == scenario.denied.pk
    profile_alone = [query["sql"] for query in captured if query["sql"].startswith('SELECT "sk_accounts_userprofile"')]
    assert profile_alone == []


@pytest.mark.parametrize("field", ["display_name", "first_name"])
@pytest.mark.parametrize("value", [{"a": 1}, ["x"], 7, True])
def test_update_my_profile_rejects_non_text_values(scenario, field, value):
    """A non-text value is refused, never stored as its Python repr (#272)."""
    service = IdentityService()
    with acting_as(scenario.member, scenario.a) as actor:
        before = service.me(actor=actor)
        with pytest.raises(InvalidInput) as error:
            service.update_my_profile(
                actor=actor,
                expected_user_revision=before.user_revision,
                expected_revision=before.revision,
                changes=((field, value),),
            )
        assert field in error.value.errors
        after = service.me(actor=actor)
    assert (after.first_name, after.profile_display_name, after.revision, after.user_revision) == (
        before.first_name,
        before.profile_display_name,
        before.revision,
        before.user_revision,
    )
