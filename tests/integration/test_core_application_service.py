"""ApplicationService of the core service API (#105)."""

import pytest

from skeleton.service_api.applications import ApplicationAdministrationService, ApplicationService, calling_app_label
from skeleton.service_api.errors import Forbidden, Unavailable
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    build_service_scenario,
)

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
NOTES = "example_notes"


def as_module(module, function):
    """Run ``function`` from a frame whose module is ``module`` (how an app's code calls the service)."""
    namespace = {"__name__": module, "function": function}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    return namespace["call"]


def test_contract_and_caller_resolution():
    assert_service_contract(ApplicationService)
    assert_service_contract(ApplicationAdministrationService)
    assert as_module("examples.notes.views", calling_app_label)() == NOTES
    assert as_module("tests.anything", calling_app_label)() is None
    assert as_module("skeleton.web.views", calling_app_label)() is None


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="applications")


def test_is_enabled_follows_activation_and_platform_availability(scenario):
    from skeleton.system.app_registry import get_app

    service = ApplicationService()
    app_id = get_app(NOTES).app_id
    with acting_as(scenario.member, scenario.a) as actor:
        assert service.is_enabled(actor=actor, app_id=app_id)
        assert service.is_enabled(actor=actor, app_id=NOTES)
        assert not service.is_enabled(actor=actor, app_id="com.example.missing")
        service.require_enabled(actor=actor, app_id=app_id)
    with scenario.application_disabled():
        with acting_as(scenario.member, scenario.a) as actor:
            assert not service.is_enabled(actor=actor, app_id=app_id)
            with pytest.raises(Unavailable):
                service.require_enabled(actor=actor, app_id=app_id)
    from skeleton.system.models import AppAvailability
    from skeleton.tenancy.context import system_context

    # The platform restricts the app to selected organizations and grants none: closed in B too.
    with system_context(reason="test:applications-availability"):
        row = AppAvailability.objects.get(app_label=NOTES)
        row.mode = "selected"
        row.save(update_fields={"mode"})
    with acting_as(scenario.member, scenario.b) as actor:
        assert not service.is_enabled(actor=actor, app_id=app_id)


def test_my_configuration_is_only_for_the_calling_app(scenario):
    service = ApplicationService()
    with acting_as(scenario.member, scenario.a) as actor:
        configuration = as_module("examples.notes.services", service.my_configuration)(actor=actor, app_label=NOTES)
        assert configuration.app_label == NOTES
        assert {item.name for item in configuration.values} == {"heading"}
        assert configuration.get("heading")
        with pytest.raises(Forbidden):
            service.my_configuration(actor=actor, app_label=NOTES)  # Called from a test module, not the app.
        with pytest.raises(Forbidden):
            as_module("skeleton.web.views", service.my_configuration)(actor=actor, app_label=NOTES)
    with scenario.application_disabled():
        with acting_as(scenario.member, scenario.a) as actor, pytest.raises(Unavailable):
            as_module("examples.notes.services", service.my_configuration)(actor=actor, app_label=NOTES)
    assert_rejects_other_actor(scenario.caller(lambda actor: service.is_enabled(actor=scenario.denied, app_id=NOTES)))


def _state(scenario, user, organization):
    with acting_as(user, organization) as actor:
        return ApplicationAdministrationService().application(actor=actor, app_label=NOTES)


def test_administration_lists_and_reads_for_the_administrator(scenario):
    service = ApplicationAdministrationService()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        labels = {state.app_label for state in service.applications(actor=admin)}
        state = service.application(actor=admin, app_label=NOTES)
    assert NOTES in labels
    assert (state.app_label, state.available, state.enabled) == (NOTES, True, True) and state.revision >= 1


def test_administration_default_deny(scenario):
    from skeleton.service_api.errors import NotFound

    service = ApplicationAdministrationService()
    with acting_as(scenario.member, scenario.a) as actor:
        for call in (
            lambda: service.applications(actor=actor),
            lambda: service.application(actor=actor, app_label=NOTES),
            lambda: service.configure(
                actor=actor, app_label=NOTES, enabled=True, configuration=(), expected_revision=1
            ),
        ):
            with pytest.raises((NotFound, Forbidden)):  # Documented: NotFound for whoever lacks apps.manage.
                call()
    with acting_as(scenario.admin_a, scenario.a) as admin, pytest.raises(NotFound):
        service.application(actor=admin, app_label="nope_app")


def test_administration_rejects_other_actor(scenario):
    service = ApplicationAdministrationService()
    for call in (
        lambda actor: service.applications(actor=scenario.denied),
        lambda actor: service.application(actor=scenario.denied, app_label=NOTES),
    ):
        assert_rejects_other_actor(scenario.caller(call, user=scenario.admin_a))


def test_configure_cas_conflict(scenario):
    from skeleton.testing.service_api import assert_cas_conflict

    state = _state(scenario, scenario.admin_a, scenario.a)
    service = ApplicationAdministrationService()

    def configure(actor):
        return service.configure(
            actor=actor, app_label=NOTES, enabled=True, configuration=(), expected_revision=state.revision
        )

    updated = assert_cas_conflict(
        scenario.caller(configure, user=scenario.admin_a), scenario.caller(configure, user=scenario.admin_a)
    )
    assert updated.revision == state.revision + 1


def test_configure_does_not_touch_other_organization(scenario):
    before_b = _state(scenario, scenario.admin_b, scenario.b)
    state = _state(scenario, scenario.admin_a, scenario.a)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        ApplicationAdministrationService().configure(
            actor=admin, app_label=NOTES, enabled=False, configuration=(), expected_revision=state.revision
        )
    after_b = _state(scenario, scenario.admin_b, scenario.b)
    assert (after_b.revision, after_b.enabled) == (before_b.revision, before_b.enabled)
