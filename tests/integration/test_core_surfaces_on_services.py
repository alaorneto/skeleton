"""Core REST and HTML surfaces are thin layers over the core service API (#110)."""

import inspect

import pytest

from skeleton.api import views as api_views
from skeleton.api.errors import service_error_response
from skeleton.service_api.errors import Conflict, Forbidden, InvalidInput, NotFound, Unavailable
from skeleton.service_api.identity import IdentityService
from skeleton.system import application_views
from skeleton.testing.service_api import acting_as, assert_service_contract, build_service_scenario
from skeleton.web import views as web_views
from skeleton.web.platform import views as platform_views

INTERNAL_PATHS = (
    "get_model",
    "update_profile(",
    "memberships_for_user",
    "application_state(",
    "configure_application(",
    ".objects.",
    "authorized_audit_queryset",
    "AuditEvent",
)
MIGRATED = (
    api_views.MeView,
    api_views.OrganizationsView,
    api_views.AuditView,
    application_views.ApplicationAPI,
    web_views.home,
    web_views.profile,
    web_views.dashboard,
    web_views.audit,
    platform_views.audit,
)


@pytest.mark.parametrize("surface", MIGRATED, ids=lambda item: item.__qualname__)
def test_migrated_surfaces_call_services_not_internals(surface):
    source = inspect.getsource(surface)
    assert "service_api" in source
    assert not [path for path in INTERNAL_PATHS if path in source]


def test_one_mapping_from_service_errors_to_http():
    cases = {
        NotFound(): (404, "not_found"),
        Unavailable(): (404, "not_found"),
        Forbidden(): (403, "permission_denied"),
        InvalidInput({"field": ["x"]}): (400, "validation_error"),
        Conflict("stale_revision"): (409, "stale_revision"),
        Conflict("quota_exceeded"): (409, "quota_exceeded"),
    }
    for error, (status, code) in cases.items():
        mapped = service_error_response(error)
        assert (mapped.status_code, mapped.detail["code"]) == (status, code), error


def test_core_if_match_resources_keep_412_for_a_stale_revision():
    from skeleton.api.errors import BoundaryError, if_match_precondition

    with pytest.raises(BoundaryError) as stale:
        with if_match_precondition():
            raise Conflict("stale_revision")
    assert (stale.value.status_code, stale.value.detail["code"]) == (412, "revision_conflict")
    with pytest.raises(Conflict), if_match_precondition():
        raise Conflict("integrity_conflict")


@pytest.mark.usefixtures("skeleton_db")
@pytest.mark.needs_example_app
def test_update_my_profile_keeps_both_revisions():

    from examples.notes.models import Note

    assert_service_contract(IdentityService)
    scenario = build_service_scenario(Note, slug="surfaces")
    service = IdentityService()
    with acting_as(scenario.member, scenario.a) as actor:
        me = service.me(actor=actor)
        updated = service.update_my_profile(
            actor=actor,
            expected_user_revision=me.user_revision,
            expected_revision=me.revision,
            changes=(("first_name", "Ana"), ("display_name", "Ana S.")),
        )
        assert (updated.first_name, updated.profile_display_name, updated.display_name) == ("Ana", "Ana S.", "Ana S.")
        assert (updated.user_revision, updated.revision) == (me.user_revision + 1, me.revision + 1)
        with pytest.raises(Conflict):
            service.update_my_profile(
                actor=actor,
                expected_user_revision=me.user_revision,
                expected_revision=updated.revision,
                changes=(("last_name", "Souza"),),
            )
        with pytest.raises(InvalidInput):
            service.update_my_profile(
                actor=actor,
                expected_user_revision=updated.user_revision,
                expected_revision=updated.revision,
                changes=(("email", "takeover@example.test"),),
            )
