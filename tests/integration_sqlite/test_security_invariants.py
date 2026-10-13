import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from uuid import uuid4

from tools.security_fixtures import actor_tenant, build_security_fixture


@pytest.fixture
def access(skeleton_db):
    assert connection.vendor == 'sqlite'
    return build_security_fixture()


def test_visibility_never_grants_writes_or_cross_tenant_reads(access):
    from examples.notes.models import Note
    from skeleton.access.policies import authorized_queryset, require_record_action
    with actor_tenant(access.bob, access.a):
        readable = authorized_queryset(Note, user=access.bob)
        assert set(readable.values_list('pk',flat=True)) == {access.shared.pk,access.grouped.pk}
        assert readable.count() == 2
        assert not readable.filter(pk=access.private.pk).exists()
        assert not readable.filter(pk=access.foreign.pk).exists()
        assert not authorized_queryset(Note,user=access.bob,action='update').exists()
        for record in (access.shared,access.grouped,access.private,access.foreign):
            with pytest.raises(PermissionDenied):
                require_record_action(record,user=access.bob,action='update')
    with actor_tenant(access.alice,access.a):
        assert set(authorized_queryset(Note,user=access.alice).values_list('pk',flat=True)) == {access.private.pk,access.shared.pk,access.grouped.pk}


def test_record_grant_requires_table_permission_and_revokes_immediately(access):
    from examples.notes.models import Note
    from skeleton.access.models import TablePermission
    from skeleton.access.services import create_record_grant, revoke_link
    from skeleton.access.policies import authorized_queryset
    with actor_tenant(access.admin,access.a):
        grant = create_record_grant(user=access.admin,data={'table':access.table,'record_sysid':access.private.pk,'membership':access.bob_membership,'action':'read'})
    with actor_tenant(access.bob,access.a):
        assert authorized_queryset(Note,user=access.bob).filter(pk=access.private.pk).exists()
    with actor_tenant(access.admin,access.a):
        permission = TablePermission.objects.get(role=access.member_role,table=access.table,action='read')
        revoke_link(permission,user=access.admin)
    with actor_tenant(access.bob,access.a):
        with pytest.raises(PermissionDenied):
            list(authorized_queryset(Note,user=access.bob))
    with actor_tenant(access.admin,access.a):
        from skeleton.access.services import grant_table_permission
        grant_table_permission(user=access.admin,data={'role':access.member_role,'table':access.table,'action':'read'})
        revoke_link(grant,user=access.admin)
    with actor_tenant(access.bob,access.a):
        assert not authorized_queryset(Note,user=access.bob).filter(pk=access.private.pk).exists()


def test_removed_group_membership_loses_visibility_next_request(access):
    from examples.notes.models import Note
    from skeleton.access.models import GroupMember
    from skeleton.access.policies import authorized_queryset
    from skeleton.access.services import revoke_link
    with actor_tenant(access.admin,access.a):
        link = GroupMember.objects.get(group=access.group,membership=access.bob_membership)
        revoke_link(link,user=access.admin)
    with actor_tenant(access.bob,access.a):
        assert not authorized_queryset(Note,user=access.bob).filter(pk=access.grouped.pk).exists()
        assert authorized_queryset(Note,user=access.bob).filter(pk=access.shared.pk).exists()


def test_administration_rejects_self_grants_and_last_admin_loss(access):
    from skeleton.access.models import Role, MemberRole
    from skeleton.access.services import create_role, delete_membership, revoke_link, LastAdministrator
    from skeleton.tenancy.models import Membership
    with actor_tenant(access.bob,access.a):
        with pytest.raises(PermissionDenied):
            create_role(user=access.bob,data={'code':'self-admin','name':'Self'})
    with actor_tenant(access.admin,access.a):
        membership = Membership.objects.get(user=access.admin)
        admin_role = Role.objects.get(code='organization_admin')
        link = MemberRole.objects.get(membership=membership,role=admin_role)
        with pytest.raises(LastAdministrator):
            revoke_link(link,user=access.admin)
        assert MemberRole.objects.filter(pk=link.pk).exists()
        with pytest.raises(LastAdministrator):
            delete_membership(membership,user=access.admin)
        membership.refresh_from_db()
        assert membership.is_active and membership.deleted_at is None


def test_last_admin_provided_only_by_group_cannot_be_removed(access):
    from skeleton.access.models import Role, MemberRole, GroupRole
    from skeleton.access.services import create_group, add_group_member, assign_group_role, revoke_link, LastAdministrator
    from skeleton.tenancy.models import Membership
    with actor_tenant(access.admin,access.a):
        membership = Membership.objects.get(user=access.admin)
        role = Role.objects.get(code='organization_admin')
        group = create_group(user=access.admin,data={'code':'admins','name':'Admins'})
        add_group_member(user=access.admin,data={'group':group,'membership':membership})
        link = assign_group_role(user=access.admin,data={'group':group,'role':role})
        direct = MemberRole.objects.get(membership=membership,role=role)
        revoke_link(direct,user=access.admin)
        with pytest.raises(LastAdministrator):
            revoke_link(link,user=access.admin)
        assert GroupRole.objects.filter(pk=link.pk).exists()


def test_missing_tenant_tombstones_and_context_restoration_fail_closed(access):
    from examples.notes.models import Note
    from skeleton.common.exceptions import MissingTenantContext
    from skeleton.tenancy.context import request_context, tenant_context, current_context, InvalidContext
    with pytest.raises(MissingTenantContext):
        Note.objects.count()
    with actor_tenant(access.alice,access.a):
        note = Note.objects.get(pk=access.private.pk)
        note.delete()
        assert not Note.objects.filter(pk=note.pk).exists()
        assert Note.objects.with_deleted().filter(pk=note.pk).exists()
        assert not Note.objects.with_deleted().filter(pk=access.foreign.pk).exists()
    assert current_context().organization_id is None
    with request_context(actor=access.alice,request_id=uuid4(),source='test:context'):
        with tenant_context(organization_id=access.a.pk,actor=access.alice,source='test:context'):
            with pytest.raises(InvalidContext):
                with tenant_context(organization_id=access.b.pk,actor=access.alice,source='test:context'):
                    pass
        assert current_context().organization_id is None
        with pytest.raises(InvalidContext):
            with tenant_context(organization_id=access.b.pk,actor=access.alice,source='test:context'):
                pass
    assert current_context().actor is None


def test_live_references_require_explicit_revocation(access):
    from skeleton.access.services import delete_group
    from skeleton.common.exceptions import LiveReferencesExist
    with actor_tenant(access.admin,access.a):
        with pytest.raises(LiveReferencesExist):
            delete_group(access.group,user=access.admin)


def test_audit_access_applies_before_list_count_and_tombstone_lookup(access):
    from skeleton.access.policies import authorized_audit_queryset
    from examples.notes.models import Note
    with actor_tenant(access.bob,access.a):
        queryset = authorized_audit_queryset(user=access.bob)
        assert not queryset.filter(record_sysid=access.private.pk).exists()
        assert not queryset.filter(record_sysid=access.foreign.pk).exists()
        assert queryset.filter(record_sysid=access.shared.pk).count() == 1
    with actor_tenant(access.alice,access.a):
        note = Note.objects.get(pk=access.private.pk)
        note.delete()
        assert authorized_audit_queryset(user=access.alice).filter(record_sysid=note.pk).count() == 2


def test_failure_to_write_audit_aborts_business_write(access):
    from examples.notes.models import Note
    from skeleton.audit.models import AuditEvent
    with actor_tenant(access.alice,access.a):
        table = connection.ops.quote_name(AuditEvent._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE TRIGGER fixture_reject_audit BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'fixture audit denial'); END")
        note = Note.objects.get(pk=access.private.pk)
        note.title = 'Must roll back'
        with pytest.raises(IntegrityError), transaction.atomic():
            note.save(update_fields=['title'])
        assert Note.objects.get(pk=note.pk).title == 'Private'
        assert AuditEvent.objects.filter(record_sysid=note.pk).count() == 1


def test_catalog_sync_preserves_uuid_human_text_without_ddl(access):
    from skeleton.system.models import TableDefinition
    from skeleton.system.synchronization import sync_table_catalog
    table = TableDefinition.objects.get(pk=access.table.pk)
    table.label = 'Human label'
    table.description = 'Human explanation'
    table.save(update_fields=['label','description'])
    revision = table.revision
    tables_before = connection.introspection.table_names()
    sync_table_catalog()
    sync_table_catalog()
    table.refresh_from_db()
    assert table.pk == access.table.pk
    assert table.label == 'Human label' and table.description == 'Human explanation'
    assert table.revision == revision
    assert connection.introspection.table_names() == tables_before
