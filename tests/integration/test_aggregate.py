"""for_each_organization (#74): one read-only context per organization, skips, limits and RLS."""

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.http import JsonResponse
from django.test import Client, override_settings
from django.urls import path, reverse

from skeleton.access.policies import authorized_queryset
from skeleton.tenancy.aggregate import LazyAggregateResult, aggregate_route, for_each_organization
from skeleton.tenancy.context import InvalidContext, system_context
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

PASSWORD = "Fixture-only-password-2026!"


@pytest.fixture
def access(skeleton_db):
    return build_security_fixture()


def note_codes(organization_id):
    from examples.notes.models import Note

    return sorted(authorized_queryset(Note, user=current_user(), action="read").values_list("code", flat=True))


_user = {}


def current_user():
    return _user["value"]


def run(user, fn, **kwargs):
    _user["value"] = user
    return for_each_organization(user, fn, source="test:aggregate", **kwargs)


def test_each_organization_runs_with_its_own_authorization(access):
    from examples.notes.models import Note

    results = run(access.alice, note_codes)
    # In A Alice reads her notes; in B her role has no table permission: B is skipped.
    assert [(row.organization_id, row.value) for row in results] == [(access.a.pk, ["grouped", "private", "shared"])]

    def rls_count(organization_id):
        return Note.all_objects.count()  # Unfiltered manager: only RLS separates the tenants.

    counts = {row.organization_id: row.value for row in run(access.bob, rls_count)}
    assert counts == {access.a.pk: 3, access.b.pk: 1}


def test_writes_inside_the_callback_fail(access):
    from examples.notes.models import Note

    def write(organization_id):
        Note(organization_id=organization_id, owner=access.alice, code="x", title="x", body="x").save()

    with pytest.raises(DatabaseError):
        run(access.alice, write)
    with actor_tenant(access.admin, access.a):
        assert not Note.objects.filter(code="x").exists()


def test_lazy_results_are_refused(access):
    from examples.notes.models import Note

    with pytest.raises(LazyAggregateResult):
        run(access.alice, lambda organization_id: Note.objects.all())
    with pytest.raises(LazyAggregateResult):
        run(access.alice, lambda organization_id: (code for code in ("a", "b")))


def test_inactive_membership_and_disabled_app_are_skipped(access):
    from skeleton.access.services import update_membership

    with actor_tenant(access.admin, access.a):
        update_membership(access.bob_membership, user=access.admin, data={"is_active": False})
    assert [row.organization_id for row in run(access.bob, lambda organization_id: 1)] == [access.b.pk]
    from skeleton.system.applications import application_state, configure_application

    with actor_tenant(access.bob, access.b):
        state = application_state("example_notes")
        configure_application(
            "example_notes",
            user=access.bob,
            enabled=False,
            configuration=state["configuration"],
            revision=state["revision"],
        )
    assert run(access.bob, lambda organization_id: 1, app_label="example_notes") == []
    assert [row.organization_id for row in run(access.bob, lambda organization_id: 1)] == [access.b.pk]


def test_limit_and_nesting(access):
    assert len(run(access.bob, lambda organization_id: 1, limit=1)) == 1
    for limit in (0, 51):
        with pytest.raises(ValueError):
            run(access.bob, lambda organization_id: 1, limit=limit)
    with system_context(reason="test:aggregate-nesting"):
        with pytest.raises(InvalidContext):
            run(access.bob, lambda organization_id: 1)


def test_permission_denied_inside_the_callback_skips_the_organization(access):
    def deny_in_a(organization_id):
        if organization_id == access.a.pk:
            raise PermissionDenied
        return "ok"

    assert [row.organization_id for row in run(access.bob, deny_in_a)] == [access.b.pk]


@aggregate_route
def aggregate_view(request):
    results = for_each_organization(request.user, lambda organization_id: str(organization_id), source="http")
    return JsonResponse({"organizations": [row.value for row in results]})


urlpatterns = [path("aggregate-test/", aggregate_view, name="aggregate-test")]


@override_settings(ROOT_URLCONF=__name__)
def test_marked_routes_run_without_the_request_transaction(access):
    from allauth.account.models import EmailAddress  # noqa: F401 - login below needs allauth loaded

    client = Client()
    client.force_login(access.bob)
    session = client.session
    session["skeleton_auth_version"] = access.bob.auth_version
    session.save()
    response = client.get(reverse("aggregate-test"))
    assert response.status_code == 200
    assert set(response.json()["organizations"]) == {str(access.a.pk), str(access.b.pk)}
    assert "X-Request-ID" in response


def test_user_context_opens_a_global_context(access):
    """The non-tenant part of an aggregate route runs as the signed-in person, without organization (#343)."""
    from types import SimpleNamespace
    from uuid import uuid4

    from skeleton.tenancy.aggregate import user_context
    from skeleton.tenancy.context import current_context

    request = SimpleNamespace(user=access.alice, request_id=uuid4())
    with user_context(request, source="test:user-context"):
        context = current_context()
        assert context.actor == access.alice.pk and context.organization_id is None and not context.system


def test_user_context_refuses_nesting(access):
    from types import SimpleNamespace
    from uuid import uuid4

    from skeleton.tenancy.aggregate import user_context

    request = SimpleNamespace(user=access.alice, request_id=uuid4())
    with actor_tenant(access.alice, access.a), pytest.raises(InvalidContext):
        with user_context(request, source="test:user-context-nested"):
            pass


def test_mfa_policy_is_enforced_in_aggregate_and_openapi_but_not_privacy_export(access, subtests):
    """#860: an org requiring MFA is omitted from aggregate reads and OpenAPI until TOTP is enrolled."""
    import io
    import json
    import zipfile

    from allauth.mfa.models import Authenticator

    from skeleton.accounts.privacy import export_my_data
    from skeleton.tenancy.models import OrganizationSettings

    with system_context(reason="test:mfa-policy", organization_id=access.a.pk):
        OrganizationSettings.objects.update_or_create(organization_id=access.a.pk, defaults={"mfa_policy": "all"})

    with subtests.test("aggregate omits org A without TOTP"):
        assert [row.organization_id for row in run(access.alice, lambda organization_id: 1)] == [access.b.pk]

    with subtests.test("OpenAPI omits tables of org A while MFA is pending"):
        client = Client()
        client.force_login(access.alice)
        session = client.session
        session["skeleton_auth_version"] = access.alice.auth_version
        session.save()
        before = client.get("/api/v1/openapi.json")
        assert before.status_code == 200
        assert not any("example_notes.note" in p for p in before.json()["paths"])

    with subtests.test("LGPD personal export still includes org A without TOTP"):
        payload = export_my_data(access.alice)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            exported = json.loads(archive.read("dados.json"))
        exported_orgs = {item["sysid"] for item in exported["organizations"]}
        assert str(access.a.pk) in exported_orgs and str(access.b.pk) in exported_orgs

    with subtests.test("enrolling TOTP restores org A in aggregate and OpenAPI"):
        Authenticator.objects.create(user=access.alice, type=Authenticator.Type.TOTP, data={})
        assert [row.organization_id for row in run(access.alice, lambda organization_id: 1)] == [
            access.a.pk,
            access.b.pk,
        ]
        client = Client()
        client.force_login(access.alice)
        session = client.session
        session["skeleton_auth_version"] = access.alice.auth_version
        session.save()
        after = client.get("/api/v1/openapi.json")
        assert after.status_code == 200
        assert any("example_notes.note" in p for p in after.json()["paths"])


def test_for_each_external_organization_validates_limit_site_and_request_id(access):
    """#860: for_each_external_organization validates limit, respects site scope and propagates request_id."""
    from uuid import uuid4

    from skeleton.tenancy.aggregate import for_each_external_organization
    from skeleton.tenancy.context import current_context
    from skeleton.tenancy.models import ExternalAccess, ExternalAccessPolicy
    from skeleton.tenancy.routing import site_scope

    for limit in (0, 51):
        with pytest.raises(ValueError):
            for_each_external_organization(access.alice, lambda oid: oid, limit=limit)

    for org in (access.a, access.b):
        with system_context(reason="test:ext-policy", organization_id=org.pk):
            ExternalAccessPolicy.objects.update_or_create(organization_id=org.pk, defaults={"enabled": True})
            ExternalAccess.objects.create(
                organization_id=org.pk,
                user=access.alice,
                app_label="example_notes",
                is_active=True,
            )

    req_id = uuid4()
    seen = for_each_external_organization(
        access.alice,
        lambda oid: (oid, current_context().request_id),
        request_id=req_id,
    )
    assert [(row.organization_id, row.value[1]) for row in seen] == [(access.a.pk, req_id), (access.b.pk, req_id)]

    with site_scope(access.a.pk):
        scoped = for_each_external_organization(access.alice, lambda oid: oid, request_id=req_id)
    assert [row.organization_id for row in scoped] == [access.a.pk]
