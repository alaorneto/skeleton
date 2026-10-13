"""These tests fail prerequisites rather than presenting SQLite as PostgreSQL proof."""
import pytest
from django.db import connection

from tools.integration_database import disposable_database_pair
from tools.postgres_assertions import assert_runtime_security
from tools.security_fixtures import build_security_fixture, actor_tenant


def test_actual_runtime_rls_guards_and_privileges(skeleton_db):
    pair = disposable_database_pair()
    assert connection.vendor == 'postgresql', 'TEST_DATABASE_URL must select a real PostgreSQL fixture.'
    assert_runtime_security(pair, build_security_fixture())


def test_orm_policies_use_runtime_not_owner(skeleton_db):
    disposable_database_pair()
    assert connection.vendor == 'postgresql'
    from examples.notes.models import Note
    from skeleton.access.policies import authorized_queryset
    fixture = build_security_fixture()
    with connection.cursor() as cursor:
        cursor.execute('SELECT current_user')
        assert cursor.fetchone()[0] == 'skeleton_runtime'
    with actor_tenant(fixture.bob, fixture.a):
        ids = set(authorized_queryset(Note, user=fixture.bob).values_list('sysid', flat=True))
        assert ids == {fixture.shared.pk, fixture.grouped.pk}
        assert not Note.all_objects.filter(pk=fixture.foreign.pk).exists()
    assert not Note.all_objects.exists()


def test_deployment_check_detects_extra_permissive_policy(skeleton_db):
    import psycopg
    from psycopg import sql
    from django.core.management import call_command, CommandError
    from examples.notes.models import Note
    pair = disposable_database_pair()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE POLICY fixture_forbidden_policy ON skeleton.{} TO skeleton_runtime USING (true) WITH CHECK (true)').format(sql.Identifier(Note._meta.db_table)))
    with pytest.raises(CommandError):
        call_command('check_skeleton_schema', database='default')


def test_deployment_check_detects_missing_guard(skeleton_db):
    import psycopg
    from psycopg import sql
    from django.core.management import call_command, CommandError
    from examples.notes.models import Note
    pair = disposable_database_pair()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(sql.SQL('DROP TRIGGER {} ON skeleton.{}').format(sql.Identifier(Note._meta.db_table+'_sk_guard'), sql.Identifier(Note._meta.db_table)))
    with pytest.raises(CommandError):
        call_command('check_skeleton_schema', database='default')


def test_concurrent_invitation_acceptance_has_exactly_one_winner(skeleton_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from urllib.parse import urlsplit
    from django.core import mail
    from django.db import connections
    from django.test import RequestFactory
    from django.urls import resolve
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.invitations import InvalidInvitation, accept_invitation, issue_invitation, validate_invitation
    from skeleton.tenancy.models import Invitation, Membership
    disposable_database_pair()
    assert connection.vendor == 'postgresql'
    fixture = build_security_fixture()
    with actor_tenant(fixture.admin, fixture.a):
        invitation = issue_invitation(user=fixture.admin, email=fixture.outsider.email,
                                      role=fixture.member_role, request=RequestFactory().get('/'))
    url = next(word for word in mail.outbox[-1].body.split() if word.startswith('http'))
    token = resolve(urlsplit(url).path).kwargs['token']
    ready = Barrier(2)

    def accept():
        try:
            with connections['default'].cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute('SELECT pg_backend_pid()')
                pid = cursor.fetchone()[0]
            proof = validate_invitation(fixture.a.pk, token)
            ready.wait(timeout=10)
            try:
                membership = accept_invitation(user=fixture.outsider, organization_id=fixture.a.pk, proof=proof)
                return pid, membership.pk
            except InvalidInvitation:
                return pid, None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(accept) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert len({pid for pid, _ in results}) == 2
    winners = [membership for _, membership in results if membership is not None]
    assert len(winners) == 1
    with system_context(reason='test:invitation-race', organization_id=fixture.a.pk):
        assert set(Membership.objects.filter(user=fixture.outsider).values_list('sysid', flat=True)) == set(winners)
        invitation.refresh_from_db()
        assert invitation.accepted_by == fixture.outsider.pk
        assert invitation.accepted_at is not None


@pytest.mark.parametrize('delete', [False, True])
def test_global_user_lifecycle_preserves_last_admin_across_organizations(skeleton_db, delete):
    from uuid import uuid4
    from skeleton.accounts.services import bootstrap_identity, deactivate_user
    from skeleton.access.models import Role
    from skeleton.access.services import LastAdministrator, provision_membership
    from skeleton.tenancy.context import request_context, system_context
    from skeleton.tenancy.models import Membership
    disposable_database_pair()
    assert connection.vendor == 'postgresql'
    fixture = build_security_fixture()
    platform, _ = bootstrap_identity(email='platform@lifecycle.example.test', organization='lifecycle-platform',
                                     name='Lifecycle platform', password='Fixture-lifecycle-password-2026!')
    version = fixture.bob.auth_version
    with pytest.raises(LastAdministrator):
        with request_context(actor=platform, request_id=uuid4(), source='test:global-lifecycle'):
            deactivate_user(fixture.bob, delete=delete)
    fixture.bob.refresh_from_db()
    assert fixture.bob.is_active and fixture.bob.deleted_at is None
    assert fixture.bob.auth_version == version
    with system_context(reason='test:alternate-admin', organization_id=fixture.b.pk):
        role = Role.objects.get(code='organization_admin')
        provision_membership(organization=fixture.b, user=fixture.alice, role=role)
    with request_context(actor=platform, request_id=uuid4(), source='test:global-lifecycle'):
        changed = deactivate_user(fixture.bob, delete=delete)
    assert changed.pk == fixture.bob.pk and not changed.is_active
    assert (changed.deleted_at is not None) is delete
    assert changed.auth_version > version
    with system_context(reason='test:retained-memberships', organization_id=fixture.b.pk):
        assert Membership.objects.filter(user=fixture.bob, deleted_at__isnull=True).exists()
