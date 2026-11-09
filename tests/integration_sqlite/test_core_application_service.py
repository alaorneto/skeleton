"""ApplicationService of the core service API (#105)."""

import pytest

from skeleton.service_api.applications import ApplicationService, calling_app_label
from skeleton.service_api.errors import Forbidden, Unavailable
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
NOTES = "example_notes"


def as_module(module, function):
    """Run ``function`` from a frame whose module is ``module`` (how an app's code calls the service)."""
    namespace = {"__name__": module, "function": function}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    return namespace["call"]


def test_contract_and_caller_resolution():
    assert_service_contract(ApplicationService)
    assert as_module("examples.notes.views", calling_app_label)() == NOTES
    assert as_module("tests.anything", calling_app_label)() is None
    assert as_module("skeleton.web.views", calling_app_label)() is None


@pytest.fixture
def scenario():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
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
        AppAvailability(app_label=NOTES, mode="selected").save()
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
