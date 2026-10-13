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


@pytest.mark.parametrize('surface', ['api-patch', 'api-delete', 'html-edit', 'html-delete'])
def test_admin_mutation_after_concurrent_membership_post_conflicts_without_deadlock(skeleton_db, surface):
    disposable_database_pair()
    assert connection.vendor == 'postgresql'
    fixture = build_security_fixture()
    clients = [Client(raise_request_exception=False), Client(raise_request_exception=False)]
    for client in clients:
        response = client.post('/accounts/login/', {
            'login': fixture.admin.email, 'password': 'Fixture-only-password-2026!',
        })
        assert response.status_code == 302
    collection = f'/api/v1/o/{fixture.a.pk}/memberships/'
    detail = collection + str(fixture.bob_membership.pk) + '/'
    etag = clients[1].get(detail)['ETag']
    with actor_tenant(fixture.admin, fixture.a):
        original_revision = Membership.objects.get(pk=fixture.bob_membership.pk).revision
    organization_locked, mutation_started = Event(), Event()

    def request(kind):
        def observe(execute, sql, params, many, context):
            if 'FOR UPDATE' in sql:
                if kind == 'create' and Organization._meta.db_table in sql and not organization_locked.is_set():
                    result = execute(sql, params, many, context)
                    organization_locked.set()
                    assert mutation_started.wait(10), 'Mutation did not attempt its first lock.'
                    return result
                if kind == 'mutate' and not mutation_started.is_set():
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
                if kind == 'create':
                    return clients[0].post(collection, json.dumps({
                        'user': str(fixture.bob.pk), 'is_active': False,
                    }), content_type='application/json').status_code
                if surface == 'api-patch':
                    return clients[1].patch(detail, json.dumps({'is_active': False}),
                                            content_type='application/json', HTTP_IF_MATCH=etag).status_code
                if surface == 'api-delete':
                    return clients[1].delete(detail, HTTP_IF_MATCH=etag).status_code
                route = 'web:access-edit' if surface == 'html-edit' else 'web:access-delete'
                path = reverse(route, kwargs={
                    'organization_id': fixture.a.pk, 'resource': 'memberships',
                    'sysid': fixture.bob_membership.pk,
                })
                return clients[1].post(path, {'revision': original_revision}).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        create = pool.submit(request, 'create')
        assert organization_locked.wait(10), 'POST did not acquire its organization lock.'
        mutate = pool.submit(request, 'mutate')
        assert create.result(timeout=20) == 201
        assert mutate.result(timeout=20) == (412 if surface.startswith('api-') else 409)

    with actor_tenant(fixture.admin, fixture.a):
        member = Membership.objects.get(pk=fixture.bob_membership.pk)
        assert not member.is_active and member.deleted_at is None
        assert member.revision == original_revision + 1
        assert not MemberRole.objects.filter(membership=member).exists()
        assert not GroupMember.objects.filter(membership=member).exists()
