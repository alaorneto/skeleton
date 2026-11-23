"""Who must enrol MFA (#740) and the gate that leaves only the MFA pages: no database."""

import json
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from skeleton.accounts import mfa
from skeleton.accounts.mfa_middleware import ALLOWED_ROUTES, SESSION_KEY, MFAEnrollmentMiddleware, allowed

ORG = "0b4c6f0e-6a4f-4a8e-9d55-0a1f6b0d2a11"


def person(**flags):
    values = {
        "is_authenticated": True,
        "is_active": True,
        "deleted_at": None,
        "is_staff": False,
        "is_superuser": False,
        "is_service_account": False,
        "pk": 1,
    }
    return SimpleNamespace(**{**values, **flags})


@pytest.fixture
def world(monkeypatch, settings):
    """Every rule off, no platform role, no organization, no capability: a test turns one on."""
    settings.SKELETON_REQUIRE_MFA = False
    settings.SKELETON_REQUIRE_ADMIN_MFA = False
    settings.SKELETON_REQUIRE_PLATFORM_ADMIN_MFA = False
    state = SimpleNamespace(organization_id=None, external=False, policy="", capabilities=False, platform=False)
    monkeypatch.setattr(
        "skeleton.accounts.platform.platform_capabilities", lambda user: {"x"} if state.platform else set()
    )
    monkeypatch.setattr(
        "skeleton.tenancy.context.current_context",
        lambda: SimpleNamespace(organization_id=state.organization_id, external=state.external),
    )
    monkeypatch.setattr(mfa, "organization_mfa_policy", lambda organization_id: state.policy)
    monkeypatch.setattr("skeleton.access.capabilities.has_any_capability", lambda user: state.capabilities)
    return state


def test_nothing_demands_mfa_by_default(world):
    assert not mfa.mfa_demanded(person())
    world.organization_id, world.capabilities = ORG, True
    assert not mfa.mfa_demanded(person())


def test_the_platform_administrator_rule_holds_without_the_administrative_one(world, settings):
    settings.SKELETON_REQUIRE_PLATFORM_ADMIN_MFA = True
    assert mfa.mfa_demanded(person(is_staff=True, is_superuser=True))
    assert not mfa.mfa_demanded(person(is_staff=True))  # Partial flags are no administrator.
    assert not mfa.mfa_demanded(person())


def test_the_every_person_rule_covers_everyone_but_service_accounts(world, settings):
    settings.SKELETON_REQUIRE_MFA = True
    assert mfa.mfa_demanded(person())
    assert not mfa.mfa_demanded(person(is_service_account=True))
    assert not mfa.mfa_demanded(SimpleNamespace(is_authenticated=False))
    assert not mfa.mfa_demanded(None)


def test_the_administrative_rule_covers_platform_roles_and_capabilities_here(world, settings):
    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    assert not mfa.mfa_demanded(person())
    world.platform = True
    assert mfa.mfa_demanded(person())
    world.platform = False
    world.capabilities = True  # Capabilities count only inside an organization.
    assert not mfa.mfa_demanded(person())
    world.organization_id = ORG
    assert mfa.mfa_demanded(person())


def test_the_organization_policy_only_tightens(world, subtests):
    world.organization_id = ORG
    for policy, capabilities, demanded in (
        ("", False, False),
        ("", True, False),
        ("admins", False, False),
        ("admins", True, True),
        ("all", False, True),
        ("all", True, True),
    ):
        with subtests.test(policy=policy, capabilities=capabilities):
            world.policy, world.capabilities = policy, capabilities
            assert mfa.mfa_demanded(person()) is demanded


def test_the_portal_and_global_pages_never_apply_an_organization_policy(world):
    world.policy, world.capabilities = "all", True
    assert not mfa.mfa_demanded(person())  # No organization in the context.
    world.organization_id, world.external = ORG, True
    assert not mfa.mfa_demanded(person())


def test_totp_satisfies_every_rule(world, settings, monkeypatch):
    settings.SKELETON_REQUIRE_MFA = True
    monkeypatch.setattr(mfa, "has_totp", lambda user: True)
    assert mfa.mfa_demanded(person()) and not mfa.mfa_enrollment_pending(person())
    monkeypatch.setattr(mfa, "has_totp", lambda user: False)
    assert mfa.mfa_enrollment_pending(person())


def test_privileged_surfaces_also_see_the_platform_administrator_rule(world, settings, monkeypatch):
    monkeypatch.setattr(mfa, "has_totp", lambda user: False)
    admin = person(is_staff=True, is_superuser=True)
    assert not mfa.mfa_required(admin)
    settings.SKELETON_REQUIRE_PLATFORM_ADMIN_MFA = True
    assert mfa.mfa_required(admin)
    assert not mfa.mfa_required(person(is_service_account=True))


def test_only_the_enrolment_routes_are_allowed(subtests):
    for path in ("/accounts/2fa/", "/accounts/2fa/totp/activate/", "/accounts/logout/", "/accounts/email/"):
        with subtests.test(path):
            assert allowed(path)
    for path in (
        "/",
        "/profile/",
        "/platform/",
        "/accounts/password/change/",
        "/accounts/2fa/totp/deactivate/",
        f"/o/{ORG}/",
        f"/api/v1/o/{ORG}/roles/",
        "/api/v1/me/",
        "/no-such-route/",
    ):
        with subtests.test(path):
            assert not allowed(path)
    assert all(not name.startswith(("platform:", "api")) for name in ALLOWED_ROUTES)


def gate(monkeypatch, *, pending=None, demand=None, totp=False):
    if pending is not None:
        demand = "platform" if pending else None
    state = SimpleNamespace(demand=demand, totp=totp)
    monkeypatch.setattr("skeleton.accounts.mfa_middleware.mfa_demand", lambda user: state.demand)
    monkeypatch.setattr("skeleton.accounts.mfa_middleware.has_totp", lambda user: state.totp)
    seen = []

    def view(request):
        seen.append(request.mfa_enrollment_pending)
        return HttpResponse("ok")

    middleware = MFAEnrollmentMiddleware(view)
    middleware.state = state
    return middleware, seen


def request(path, user=None, session=None):
    built = RequestFactory().get(path)
    built.user = user or person()
    if session is not None:
        built.session = session
    return built


def test_only_an_organization_policy_marks_the_browser_session(monkeypatch):
    session = {}
    middleware, seen = gate(monkeypatch, demand="platform")
    middleware(request("/profile/", session=session))
    assert session == {}  # The platform's rules hold every request by themselves.
    middleware.state.demand = "organization"
    assert middleware(request(f"/o/{ORG}/", session=session)).status_code == 302
    assert session == {SESSION_KEY: True}


def test_the_mark_only_keeps_the_enrolment_without_menus(monkeypatch):
    """Sent to the MFA pages by an organization, the person still walks freely elsewhere."""
    session = {SESSION_KEY: True}
    middleware, seen = gate(monkeypatch, demand=None)  # The global MFA page, outside the organization.
    assert middleware(request("/accounts/2fa/", session=session)).status_code == 200
    assert seen == [True] and session == {SESSION_KEY: True}
    assert middleware(request("/profile/", session=session)).status_code == 200  # Never held back.
    assert seen == [True, False] and session == {}  # Leaving the enrolment drops the mark.
    assert middleware(request("/accounts/2fa/", session=session)).status_code == 200
    assert seen == [True, False, False]  # A visit of one's own shows the menus.


def test_enrolling_drops_the_mark(monkeypatch):
    session = {SESSION_KEY: True}
    middleware, seen = gate(monkeypatch, demand=None, totp=True)
    middleware(request("/accounts/2fa/", session=session))
    assert seen == [False] and session == {}


def test_a_bearer_token_never_marks_a_session(monkeypatch):
    session = {}
    middleware, _ = gate(monkeypatch, demand="organization")
    token_request = request(f"/api/v1/o/{ORG}/roles/", session=session)
    token_request.api_token = object()
    assert middleware(token_request).status_code == 403
    assert session == {}


def test_the_gate_sends_html_to_the_enrolment_and_refuses_the_api(monkeypatch):
    middleware, seen = gate(monkeypatch, pending=True)
    response = middleware(request("/profile/"))
    assert response.status_code == 302 and response["Location"] == reverse("mfa_index")
    response = middleware(request(f"/api/v1/o/{ORG}/roles/"))
    assert response.status_code == 403
    assert json.loads(response.content)["code"] == "mfa_required"
    assert seen == []
    assert middleware(request("/accounts/2fa/")).status_code == 200
    assert seen == [True]


def test_the_gate_lets_everything_through_when_nothing_is_pending(monkeypatch):
    middleware, seen = gate(monkeypatch, pending=False)
    assert middleware(request("/profile/")).status_code == 200
    assert middleware(request("/profile/", SimpleNamespace(is_authenticated=False))).status_code == 200
    assert seen == [False, False]


def test_the_service_api_never_writes_the_mfa_policy():
    from skeleton.tenancy.organization_settings import _clean

    with pytest.raises(ValidationError) as error:
        _clean({"mfa_policy": "all"})
    assert "mfa_policy" in error.value.message_dict


def test_production_pins_the_platform_administrator_rule(tmp_path, monkeypatch):
    from tests.unit.test_mailers import _production

    monkeypatch.setenv("SKELETON_REQUIRE_PLATFORM_ADMIN_MFA", "false")
    result = _production(
        "smtp+tls://user:secret@mail.example:587",
        tmp_path,
        "print(settings.SKELETON_REQUIRE_PLATFORM_ADMIN_MFA)",
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "True"
