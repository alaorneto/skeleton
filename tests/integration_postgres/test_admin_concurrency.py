"""Administrative mutations acquire the organization before the target row."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from skeleton.access.models import GroupMember, MemberRole
from skeleton.tenancy.models import Membership, Organization
from tools.integration_database import disposable_database_pair
from tools.security_fixtures import actor_tenant, build_security_fixture


@pytest.mark.parametrize("surface", ["api-patch", "api-delete", "html-edit", "html-delete"])
def test_admin_mutation_after_concurrent_membership_post_conflicts_without_deadlock(skeleton_db, surface):
    disposable_database_pair()
    assert connection.vendor == "postgresql"
    fixture = build_security_fixture()
    clients = [Client(raise_request_exception=False), Client(raise_request_exception=False)]
    for client in clients:
        response = client.post(
            "/accounts/login/",
            {
                "login": fixture.admin.email,
                "password": "Fixture-only-password-2026!",
            },
        )
        assert response.status_code == 302
    collection = f"/api/v1/o/{fixture.a.pk}/memberships/"
    detail = collection + str(fixture.bob_membership.pk) + "/"
    etag = clients[1].get(detail)["ETag"]
    with actor_tenant(fixture.admin, fixture.a):
        original_revision = Membership.objects.get(pk=fixture.bob_membership.pk).revision
    organization_locked, mutation_started = Event(), Event()

    def request(kind):
        def observe(execute, sql, params, many, context):
            if "FOR UPDATE" in sql:
                if kind == "create" and Organization._meta.db_table in sql and not organization_locked.is_set():
                    result = execute(sql, params, many, context)
                    organization_locked.set()
                    assert mutation_started.wait(10), "Mutation did not attempt its first lock."
                    return result
                if kind == "mutate" and not mutation_started.is_set():
                    if Membership._meta.db_table in sql:
                        result = execute(sql, params, many, context)
                        mutation_started.set()
                        return result
                    if Organization._meta.db_table in sql:
                        mutation_started.set()
            return execute(sql, params, many, context)

        connections.close_all()
        try:
            with connection.execute_wrapper(observe):
                if kind == "create":
                    return (
                        clients[0]
                        .post(
                            collection,
                            json.dumps(
                                {
                                    "user": str(fixture.bob.pk),
                                    "is_active": False,
                                }
                            ),
                            content_type="application/json",
                        )
                        .status_code
                    )
                if surface == "api-patch":
                    return (
                        clients[1]
                        .patch(
                            detail,
                            json.dumps({"is_active": False}),
                            content_type="application/json",
                            HTTP_IF_MATCH=etag,
                        )
                        .status_code
                    )
                if surface == "api-delete":
                    return clients[1].delete(detail, HTTP_IF_MATCH=etag).status_code
                route = "web:access-edit" if surface == "html-edit" else "web:access-delete"
                path = reverse(
                    route,
                    kwargs={
                        "organization_id": fixture.a.pk,
                        "resource": "memberships",
                        "sysid": fixture.bob_membership.pk,
                    },
                )
                return clients[1].post(path, {"revision": original_revision}).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        create = pool.submit(request, "create")
        assert organization_locked.wait(10), "POST did not acquire its organization lock."
        mutate = pool.submit(request, "mutate")
        assert create.result(timeout=20) == 201
        assert mutate.result(timeout=20) == (412 if surface.startswith("api-") else 409)

    with actor_tenant(fixture.admin, fixture.a):
        member = Membership.objects.get(pk=fixture.bob_membership.pk)
        assert not member.is_active and member.deleted_at is None
        assert member.revision == original_revision + 1
        assert not MemberRole.objects.filter(membership=member).exists()
        assert not GroupMember.objects.filter(membership=member).exists()


def _platform_admins(*users):
    from skeleton.accounts.models import User
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:platform-admins"):
        for user in users:
            stored = User.objects.get(pk=user.pk)
            stored.is_staff = stored.is_superuser = True
            stored.save(update_fields=["is_staff", "is_superuser"])
        return set(User.objects.filter(is_active=True, is_staff=True, is_superuser=True).values_list("pk", flat=True))


def test_concurrent_platform_demotions_keep_one_admin(skeleton_db):
    import time
    from uuid import uuid4

    from skeleton.accounts.models import User
    from skeleton.accounts.platform import LastPlatformAdministrator
    from skeleton.accounts.services import update_platform_user
    from skeleton.tenancy.context import request_context

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    fixture = build_security_fixture()
    assert _platform_admins(fixture.alice, fixture.bob) == {fixture.alice.pk, fixture.bob.pk}
    leader_locked, follower_waiting = Event(), Event()

    def demote(user, leader):
        def observe(execute, sql, params, many, context):
            # Only the ordered lock of the administrator set, not the target row lock.
            if "FOR UPDATE" in sql and "ORDER BY" in sql and User._meta.db_table in sql:
                if leader:
                    result = execute(sql, params, many, context)
                    leader_locked.set()
                    assert follower_waiting.wait(10), "Follower did not reach the administrator lock."
                    time.sleep(0.5)
                    return result
                assert leader_locked.wait(10), "Leader did not lock the administrators."
                follower_waiting.set()
            return execute(sql, params, many, context)

        connections.close_all()
        try:
            with connection.execute_wrapper(observe):
                with request_context(actor=user, request_id=uuid4(), source="test:concurrent-platform-demotion"):
                    revision = User.objects.get(pk=user.pk).revision
                    update_platform_user(user, data={"is_staff": False}, expected_revision=revision)
            return "demoted"
        except LastPlatformAdministrator:
            return "last_platform_administrator"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(demote, fixture.alice, True)
        follower = pool.submit(demote, fixture.bob, False)
        results = [leader.result(timeout=30), follower.result(timeout=30)]
    assert results == ["demoted", "last_platform_administrator"]
    remaining = User.objects.filter(is_active=True, is_staff=True, is_superuser=True)
    assert list(remaining.values_list("pk", flat=True)) == [fixture.bob.pk]


def test_superuser_without_staff_keeps_platform_guc_off(skeleton_db):
    from uuid import uuid4

    from skeleton.accounts.models import User
    from skeleton.tenancy.context import request_context, system_context

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    fixture = build_security_fixture()
    with system_context(reason="test:partial-platform-flags"):
        partial = User.objects.get(pk=fixture.alice.pk)
        partial.is_superuser = True
        partial.save(update_fields=["is_superuser"])
    _platform_admins(fixture.bob)
    observed = {}
    for user in (User.objects.get(pk=fixture.alice.pk), User.objects.get(pk=fixture.bob.pk)):
        with request_context(actor=user, request_id=uuid4(), source="test:platform-guc"):
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('skeleton.platform_admin', true)")
                observed[user.pk] = cursor.fetchone()[0]
    assert observed == {fixture.alice.pk: "off", fixture.bob.pk: "on"}
