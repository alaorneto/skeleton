"""Assertions run on actual PostgreSQL connections, never an owner stand-in."""
from uuid import uuid4
import psycopg
from psycopg import sql


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def expect_state(connection, statement, params, state):
    try:
        with connection.transaction():
            connection.execute(statement, params)
    except psycopg.Error as exc:
        require(exc.sqlstate == state, f'Expected SQLSTATE {state}; got {exc.sqlstate}: {exc.diag.message_primary}')
    else:
        raise AssertionError(f'Expected SQLSTATE {state}; statement unexpectedly succeeded.')


def context(connection, tenant=None, actor=None):
    for name, value in {'tenant_id':tenant, 'user_id':actor, 'request_id':uuid4(),
                        'source':'test:runtime-sql', 'platform_admin':'off'}.items():
        connection.execute('SELECT set_config(%s,%s,true)', ('skeleton.'+name, str(value) if value is not None else ''))


def assert_runtime_security(pair, fixture, emit=lambda message: None):
    from django.apps import apps
    from examples.notes.models import Note
    from skeleton.audit.models import AuditEvent
    from skeleton.common.models import OrganizationRecord
    from skeleton.tenancy.models import Membership
    note = sql.Identifier('skeleton', Note._meta.db_table)
    membership = sql.Identifier('skeleton', Membership._meta.db_table)
    audit = sql.Identifier('skeleton', AuditEvent._meta.db_table)
    with psycopg.connect(pair.runtime, autocommit=True) as connection:
        role = connection.execute('SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
        require(role == ('skeleton_runtime', False, False), 'Assertions must use non-superuser NOBYPASSRLS runtime.')
        owned = connection.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='skeleton' AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)").fetchone()[0]
        require(owned == 0, 'Runtime must not own schema objects.')
        for privileged in ('skeleton_owner', 'skeleton_audit_writer'):
            expect_state(connection, sql.SQL('SET ROLE {}').format(sql.Identifier(privileged)), None, '42501')
        emit('runtime: non-superuser, NOBYPASSRLS, no ownership or privileged SET ROLE')
        for model in apps.get_models():
            if not issubclass(model, OrganizationRecord) or model._meta.abstract:
                continue
            table = model._meta.db_table
            flags = connection.execute('SELECT c.relrowsecurity,c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s', ('skeleton',table)).fetchone()
            require(flags == (True, True), f'{table}: ENABLE/FORCE RLS missing.')
            policies = connection.execute('SELECT policyname,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname=%s AND tablename=%s', ('skeleton',table)).fetchall()
            expected = {'skeleton_membership_select','skeleton_membership_insert','skeleton_membership_update','skeleton_membership_delete'} if model is Membership else {'skeleton_tenant'}
            require({p[0] for p in policies} == expected, f'{table}: unexpected policy inventory {policies}.')
            require(all(p[1] == ['skeleton_runtime'] for p in policies), f'{table}: unexpected policy role.')
            for policy in policies:
                if policy[2] in ('ALL','INSERT','UPDATE'):
                    require(policy[4] and 'skeleton.tenant_id' in policy[4] and 'skeleton.user_id' not in policy[4], f'{table}: write policy must check only tenant.')
        emit('runtime: exact tenant policy names/roles and ENABLE/FORCE inventory')
        count = sql.SQL('SELECT count(*) FROM {}').format(note)
        require(connection.execute(count).fetchone()[0] == 0, 'No context must expose zero tenant rows.')
        # Clone columns from a materialized fixture; inserts do not depend on SELECT visibility.
        columns = [field.column for field in Note._meta.concrete_fields]
        data = {}
        for field in Note._meta.concrete_fields:
            data[field.column] = field.value_from_object(fixture.private)
        insert = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(note,
            sql.SQL(',').join(map(sql.Identifier,columns)), sql.SQL(',').join(sql.Placeholder() for _ in columns))
        def values(organization, **replacements):
            row = dict(data, sysid=uuid4(), organization_id=organization, code='sql-'+uuid4().hex)
            row.update(replacements)
            return [row[column] for column in columns]
        expect_state(connection, insert, values(fixture.a.pk), '42501')
        emit('runtime: missing context SELECT=0 and INSERT=42501')
        with connection.transaction():
            context(connection, fixture.a.pk, fixture.alice.pk)
            ids = {row[0] for row in connection.execute(sql.SQL('SELECT sysid FROM {}').format(note))}
            require(fixture.private.pk in ids and fixture.foreign.pk not in ids, 'Tenant A reads only A.')
            update = sql.SQL('UPDATE {} SET title=%s,revision=revision+1 WHERE sysid=%s').format(note)
            require(connection.execute(update, ('forbidden', fixture.foreign.pk)).rowcount == 0, 'UPDATE B must affect zero rows.')
            require(connection.execute(sql.SQL('UPDATE {} SET deleted_at=clock_timestamp(),revision=revision+1 WHERE sysid=%s').format(note), (fixture.foreign.pk,)).rowcount == 0, 'Soft DELETE B must affect zero rows.')
            expect_state(connection, sql.SQL('DELETE FROM {} WHERE sysid=%s').format(note), (fixture.foreign.pk,), '42501')
            expect_state(connection, insert, values(fixture.b.pk), '42501')
            # A cross-tenant group is supplied by the harness, not resolved under A.
            require(hasattr(fixture, 'foreign_group'), 'Fixture requires a real B group for composite-FK proof.')
            expect_state(connection, insert, values(fixture.a.pk, visibility='group', owning_group_id=fixture.foreign_group.pk), '23503')
            expect_state(connection, sql.SQL('UPDATE {} SET organization_id=%s,revision=revision+1 WHERE sysid=%s').format(note), (fixture.b.pk,fixture.private.pk), '23514')
            # Self-discovery must never become cross-tenant mutation rights.
            own = connection.execute(sql.SQL('SELECT organization_id FROM {} WHERE user_id=%s').format(membership), (fixture.alice.pk,)).fetchall()
            require({r[0] for r in own} == {fixture.a.pk,fixture.b.pk}, 'Membership discovery must reveal only the actor memberships.')
            require(connection.execute(sql.SQL('UPDATE {} SET is_active=false,revision=revision+1 WHERE organization_id=%s AND user_id=%s').format(membership), (fixture.b.pk,fixture.alice.pk)).rowcount == 0, 'Self-discovery must not authorize B UPDATE under A.')
            for statement in (sql.SQL('TRUNCATE {}').format(note), sql.SQL('ALTER TABLE {} ADD COLUMN forbidden integer').format(note),
                sql.SQL('INSERT INTO {} DEFAULT VALUES').format(audit), sql.SQL('UPDATE {} SET source=%s').format(audit), sql.SQL('DELETE FROM {}').format(audit)):
                expect_state(connection, statement, ('forbidden',) if 'UPDATE' in statement.as_string(connection) else None, '42501')
        require(connection.execute(count).fetchone()[0] == 0, 'COMMIT must clear LOCAL tenant context.')
        emit('runtime: A/B DML, cross-tenant FK, immutable org, membership write boundary, denied privileged DML/DDL')
        try:
            with connection.transaction():
                context(connection, fixture.b.pk, fixture.bob.pk)
                require(connection.execute(count).fetchone()[0] == 1, 'B sees exactly its fixture note.')
                raise RuntimeError('intentional rollback')
        except RuntimeError:
            pass
        require(connection.execute(count).fetchone()[0] == 0, 'ROLLBACK must clear LOCAL tenant context.')
        with connection.transaction():
            context(connection, 'malformed', fixture.alice.pk)
            expect_state(connection, count, None, '22P02')
        with psycopg.connect(pair.runtime, autocommit=True) as second:
            with connection.transaction(), second.transaction():
                context(connection,fixture.a.pk,fixture.alice.pk)
                context(second,fixture.b.pk,fixture.bob.pk)
                require(connection.execute(count).fetchone()[0] == 3, 'Connection A context crossed.')
                require(second.execute(count).fetchone()[0] == 1, 'Connection B context crossed.')
        emit('runtime: COMMIT/ROLLBACK resets, malformed UUID errors, interleaved connections isolated')
    # This one assertion deliberately uses the offline owner, explicitly not RLS proof.
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        with owner.transaction():
            expect_state(owner, sql.SQL('UPDATE {} SET organization_id=%s,revision=revision+1 WHERE sysid=%s').format(note), (fixture.b.pk,fixture.private.pk), '23514')
        emit('owner guard only: organization immutability also holds independently of RLS')
    # Fault injection uses owner solely to change a fixture grant. The failed
    # application write and all persistence assertions remain runtime.
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(sql.SQL('REVOKE INSERT ON {} FROM skeleton_audit_writer').format(audit))
        try:
            with psycopg.connect(pair.runtime, autocommit=True) as runtime:
                with runtime.transaction():
                    context(runtime, fixture.a.pk, fixture.alice.pk)
                    expect_state(runtime, sql.SQL('UPDATE {} SET title=%s,revision=revision+1 WHERE sysid=%s').format(note),
                        ('Must not persist without audit', fixture.private.pk), '42501')
                    stored = runtime.execute(sql.SQL('SELECT title,revision FROM {} WHERE sysid=%s').format(note), (fixture.private.pk,)).fetchone()
                    require(stored == (fixture.private.title,fixture.private.revision), 'Audit INSERT failure must roll back the business change.')
                    require(runtime.execute(sql.SQL('SELECT count(*) FROM {} WHERE record_sysid=%s').format(audit), (fixture.private.pk,)).fetchone()[0] == 1,
                        'Audit INSERT failure must not persist a partial event.')
        finally:
            owner.execute(sql.SQL('GRANT INSERT ON {} TO skeleton_audit_writer').format(audit))
    emit('runtime: revoked trigger INSERT aborts business change and event atomically; fixture grant restored')
