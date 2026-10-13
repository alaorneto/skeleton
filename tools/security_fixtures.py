"""Discardable fixture records built through public identity/provisioning services."""
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4


@dataclass
class SecurityFixture:
    admin: object
    alice: object
    bob: object
    outsider: object
    a: object
    b: object
    private: object
    shared: object
    grouped: object
    foreign: object
    group: object
    alice_membership: object
    bob_membership: object
    member_role: object
    table: object
    foreign_group: object


@contextmanager
def actor_tenant(user, organization):
    from skeleton.tenancy.context import request_context, tenant_context
    with request_context(actor=user, request_id=uuid4(), source='test:security'):
        with tenant_context(organization_id=organization.pk, actor=user, source='test:security'):
            yield


def build_security_fixture():
    from allauth.account.models import EmailAddress
    from examples.notes.models import Note
    from skeleton.accounts.services import create_identity, create_organization
    from skeleton.access.models import AccessGroup, GroupMember, Role, TablePermission, Action
    from skeleton.access.services import provision_membership
    from skeleton.system.models import TableDefinition
    from skeleton.system.synchronization import sync_table_catalog
    from skeleton.tenancy.context import system_context
    with system_context(reason='fixture:identities'):
        users = []
        for name in ('admin', 'alice', 'bob', 'outsider'):
            user = create_identity(email=f'{name}@security.example.test', password='Fixture-only-password-2026!')
            EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
            users.append(user)
        admin, alice, bob, outsider = users
    a = create_organization(owner=admin, slug='security-a', name='Security A')
    b = create_organization(owner=bob, slug='security-b', name='Security B')
    sync_table_catalog()
    table = TableDefinition.objects.get(model_label=Note._meta.label_lower)
    with system_context(reason='fixture:tenant-a', organization_id=a.pk):
        member = Role.objects.get(code='member')
        alice_membership = provision_membership(organization=a, user=alice, role=member)
        bob_membership = provision_membership(organization=a, user=bob, role=member)
        for action in Action.values:
            TablePermission(organization=a, role=member, table=table, action=action).save()
        group = AccessGroup(organization=a, code='readers', name='Readers')
        group.save()
        GroupMember(organization=a, group=group, membership=bob_membership).save()
        private = Note(organization=a, owner=alice, code='private', title='Private', body='secret body private')
        shared = Note(organization=a, owner=alice, code='shared', title='Shared', body='secret body shared', visibility='organization')
        grouped = Note(organization=a, owner=alice, code='grouped', title='Grouped', body='secret body grouped', visibility='group', owning_group=group)
        for note in (private, shared, grouped):
            note.save()
    with system_context(reason='fixture:tenant-b', organization_id=b.pk):
        member_b = Role.objects.get(code='member')
        provision_membership(organization=b, user=alice, role=member_b)
        foreign_group = AccessGroup(organization=b, code='foreign-readers', name='Foreign readers')
        foreign_group.save()
        foreign = Note(organization=b, owner=bob, code='foreign', title='Foreign', body='secret body foreign')
        foreign.save()
    return SecurityFixture(admin, alice, bob, outsider, a, b, private, shared, grouped, foreign,
        group, alice_membership, bob_membership, member, table, foreign_group)
