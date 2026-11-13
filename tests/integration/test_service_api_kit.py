"""The service API test kit rejects unsafe services (#321): runs on PostgreSQL with the runtime role.

Each test hands a helper a defective "service" (a callable that misbehaves the way a real one
could) and expects the helper to fail; the real ``NoteService`` proves the codes it relies on.
"""

import pytest

from examples.notes.models import Note
from examples.notes.service_api import NoteService
from skeleton.service_api import Forbidden, InvalidInput, Unavailable
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

pytestmark = pytest.mark.needs_example_app


@pytest.fixture
def scenario(skeleton_db):
    return build_service_scenario(Note, slug="service-kit")


def test_actor_mismatch_has_its_own_code(scenario):
    with acting_as(scenario.member, scenario.a), pytest.raises(Forbidden) as error:
        NoteService().note_count(actor=scenario.admin_a)
    assert error.value.code == "actor_mismatch"
    assert_rejects_other_actor(scenario.caller(lambda actor: NoteService().note_count(actor=scenario.admin_a)))


def test_kit_rejects_a_service_that_ignores_the_actor(scenario):
    def ignores_actor(actor):
        return 0  # Answers whoever the caller claims to be.

    with pytest.raises(AssertionError, match="expected Forbidden"):
        assert_rejects_other_actor(scenario.caller(ignores_actor))


def test_kit_rejects_forbidden_for_lack_of_permission_as_actor_mismatch(scenario):
    """A denied person as "the other actor" is refused for lack of permission, which proves nothing."""

    def denied_for_permission(actor):
        raise Forbidden()

    with pytest.raises(AssertionError, match="another reason"):
        assert_rejects_other_actor(scenario.caller(denied_for_permission))


def test_kit_rejects_any_error_as_isolation(scenario):
    foreign = scenario.create(scenario.b, code="kit-foreign", title="Foreign")
    for error in (InvalidInput({}), Forbidden()):

        def fails(actor, error=error):
            raise error

        with pytest.raises(AssertionError, match="Expected a result or NotFound"):
            assert_tenant_isolated(scenario.caller(fails), foreign=[foreign])
    with pytest.raises(AssertionError, match="Expected count 0"):
        assert_tenant_isolated(scenario.caller(lambda actor: 1), foreign=[foreign], expected_count=0)
    # The real service: counts only its own organization.
    assert_tenant_isolated(
        scenario.caller(lambda actor: NoteService().note_count(actor=actor)), foreign=[foreign], expected_count=0
    )


def test_kit_rejects_a_call_already_unavailable(scenario):
    def always_unavailable(actor):
        raise Unavailable()

    with pytest.raises(AssertionError, match="already Unavailable"):
        assert_unavailable_when_disabled(scenario.caller(always_unavailable), disabled=scenario.application_disabled)
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: NoteService().note_count(actor=actor)), disabled=scenario.application_disabled
    )


def test_activate_app_refuses_a_disabled_dependency(scenario, monkeypatch):
    from skeleton.system import app_registry
    from skeleton.testing.applications import activate_app

    real = app_registry.required_labels
    monkeypatch.setattr(
        app_registry,
        "required_labels",
        lambda label: ["phantom_dependency"] if label == "example_notes" else real(label),
    )
    with pytest.raises(ValueError, match="phantom_dependency"):
        activate_app("example_notes", scenario.a.pk)


def test_nested_apps_open_by_default_keeps_the_outer_signal(skeleton_db):
    from skeleton.accounts.services import create_identity
    from skeleton.system.models import AppInstallation
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.services import create_organization
    from skeleton.testing.applications import apps_open_by_default

    with system_context(reason="test:kit-owner"):
        owner = create_identity(email="owner@service-kit-nested.example.test", password="Kit-nested-Password-2026!")
    with apps_open_by_default(["example_notes"]):
        with apps_open_by_default(["example_notes"]):
            pass
        organization = create_organization(owner=owner, slug="service-kit-nested", name="Nested")
    with system_context(reason="test:kit-installations", organization_id=organization.pk):
        assert AppInstallation.objects.filter(app_label="example_notes", enabled=True).exists()


def test_scenario_password_logs_in(scenario):
    from tests import support

    assert scenario.password
    support.login(scenario.member, password=scenario.password)
