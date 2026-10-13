"""Consumer regressions for RD05, SBR01 and SBR04 (both database fixtures)."""
import json
import re
from urllib.parse import urlsplit

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory
from django.urls import reverse

from tools.security_fixtures import actor_tenant, build_security_fixture

PASSWORD = 'Fixture-only-password-2026!'


@pytest.fixture
def access(skeleton_db):
    return build_security_fixture()


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse('account_login'))
    response = client.post(reverse('account_login'), {
        'login': user.email, 'password': PASSWORD,
        'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
    })
    assert response.status_code == 302
    return client


def membership_url(access, detail=False):
    kwargs = {'organization_id': access.a.pk, 'resource': 'memberships'}
    if detail:
        kwargs['sysid'] = access.bob_membership.pk
    return reverse('api:access-detail' if detail else 'api:access-list', kwargs=kwargs)


def mutate(client, method, url, data=None, revision=None):
    headers = {'HTTP_X_CSRFTOKEN': client.cookies['csrftoken'].value}
    if revision is not None:
        headers['HTTP_IF_MATCH'] = f'"{revision}"'
    return getattr(client, method)(url, data=json.dumps(data or {}), content_type='application/json', **headers)


@pytest.mark.parametrize('method', ['post', 'patch'])
def test_rd05_post_and_patch_revoke_all_membership_acl(access, method):
    from skeleton.access.models import GroupMember, MemberRole, RecordGrant
    from skeleton.access.services import create_record_grant
    from skeleton.tenancy.models import Membership
    with actor_tenant(access.admin, access.a):
        create_record_grant(user=access.admin, data={
            'table': access.table, 'record_sysid': access.private.pk,
            'membership': access.bob_membership, 'action': 'read',
        })
        links = {model: list(model.objects.filter(membership=access.bob_membership).values_list('pk', flat=True))
                 for model in (MemberRole, GroupMember, RecordGrant)}
        assert all(links.values())
    client = login(access.admin)
    data = {'is_active': False}
    if method == 'post':
        data['user'] = str(access.bob.pk)
    response = mutate(client, method, membership_url(access, detail=method == 'patch'), data, access.bob_membership.revision)
    assert response.status_code == (201 if method == 'post' else 200)
    with actor_tenant(access.admin, access.a):
        membership = Membership.objects.get(pk=access.bob_membership.pk)
        assert not membership.is_active
        for model, ids in links.items():
            assert not model.objects.filter(pk__in=ids).exists()
            assert model.objects.with_deleted().filter(pk__in=ids, deleted_at__isnull=False).count() == len(ids)
    activated = mutate(client, 'post', membership_url(access), {'user': str(access.bob.pk), 'is_active': True})
    assert activated.status_code == 201
    with actor_tenant(access.admin, access.a):
        for model, ids in links.items():
            assert not model.objects.filter(pk__in=ids).exists()


def test_rd05_patch_stale_revision_and_last_admin_remain_protected(access):
    from skeleton.tenancy.models import Membership
    client = login(access.admin)
    revision = access.bob_membership.revision
    assert mutate(client, 'patch', membership_url(access, True), {'is_active': False}, revision).status_code == 200
    assert mutate(client, 'patch', membership_url(access, True), {'is_active': True}, revision).status_code == 412
    assert mutate(client, 'post', membership_url(access), {'user': str(access.admin.pk), 'is_active': False}).status_code == 409
    with actor_tenant(access.admin, access.a):
        assert Membership.objects.get(user=access.admin).is_active
        assert not Membership.objects.get(pk=access.bob_membership.pk).is_active


@pytest.mark.parametrize('active', [True, False])
def test_sbr01_membership_post_restore_persists_requested_state_once(access, active):
    from skeleton.access.services import delete_membership
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.models import Membership
    with actor_tenant(access.admin, access.a):
        delete_membership(access.bob_membership, user=access.admin)
        previous = Membership.objects.with_deleted().get(pk=access.bob_membership.pk)
        revision = previous.revision
        events = AuditEvent.objects.filter(record_sysid=previous.pk).count()
    client = login(access.admin)
    response = mutate(client, 'post', membership_url(access), {'user': str(access.bob.pk), 'is_active': active})
    assert response.status_code == 201
    with actor_tenant(access.admin, access.a):
        restored = Membership.objects.get(pk=previous.pk)
        assert restored.is_active is active
        assert response.json()['is_active'] is restored.is_active
        assert restored.revision == revision + 1
        assert AuditEvent.objects.filter(record_sysid=restored.pk).count() == events + 1


def test_sbr01_reinvitation_restores_same_membership_and_consumes_once(access):
    from skeleton.access.models import MemberRole
    from skeleton.access.services import delete_membership
    from skeleton.tenancy.invitations import InvalidInvitation, accept_invitation, issue_invitation
    from skeleton.tenancy.models import Invitation, Membership
    with actor_tenant(access.admin, access.a):
        delete_membership(access.bob_membership, user=access.admin)
        revision = Membership.objects.with_deleted().get(pk=access.bob_membership.pk).revision
        invitation = issue_invitation(user=access.admin, email=access.bob.email, role=access.member_role,
                                      request=RequestFactory().get('/', HTTP_HOST='testserver'))
    message = next(message for message in reversed(mail.outbox) if invitation.email in message.to)
    path = urlsplit(re.search(r'https?://[^\s]+', message.body).group()).path
    token = path.rstrip('/').rsplit('/', 1)[-1]
    result = accept_invitation(user=access.bob, organization_id=access.a.pk, token=token)
    assert result.pk == access.bob_membership.pk
    with actor_tenant(access.admin, access.a):
        membership = Membership.objects.get(pk=result.pk)
        assert membership.is_active
        assert membership.revision == revision + 1
        assert MemberRole.objects.filter(membership=membership, role=access.member_role).count() == 1
        accepted = Invitation.objects.get(pk=invitation.pk)
        assert accepted.accepted_at is not None and accepted.accepted_by == access.bob.pk
        accepted_revision = accepted.revision
    with pytest.raises(InvalidInvitation):
        accept_invitation(user=access.bob, organization_id=access.a.pk, token=token)
    with actor_tenant(access.admin, access.a):
        assert Invitation.objects.get(pk=invitation.pk).revision == accepted_revision
        assert MemberRole.objects.filter(membership_id=result.pk, role=access.member_role).count() == 1


def test_sbr04_inactive_membership_can_be_deleted_without_cross_tenant_bypass(access):
    from skeleton.access.services import delete_membership, update_membership
    from skeleton.tenancy.models import Membership
    with actor_tenant(access.bob, access.b):
        foreign = Membership.objects.get(user=access.alice)
    client = login(access.admin)
    response = mutate(client, 'patch', membership_url(access, True), {'is_active': False}, access.bob_membership.revision)
    assert response.status_code == 200
    deleted = mutate(client, 'delete', membership_url(access, True), revision=response.json()['revision'])
    assert deleted.status_code == 204
    with actor_tenant(access.admin, access.a):
        record = Membership.objects.with_deleted().get(pk=access.bob_membership.pk)
        assert record.deleted_at is not None and not record.is_active
        with pytest.raises(ValidationError):
            delete_membership(foreign, user=access.admin)
    with actor_tenant(access.bob, access.b):
        assert Membership.objects.get(pk=foreign.pk).is_active
