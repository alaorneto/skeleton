import hashlib
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

from skeleton.accounts.services import bootstrap_identity
from skeleton.system.infrastructure_snapshot import export_inventory
from skeleton.system.retained_schema import export_schema
from skeleton.tenancy.context import tenant_context

pytestmark = pytest.mark.usefixtures('skeleton_db')


def test_retained_schema_impact_counts_each_tenant_once(tmp_path):
    from examples.notes.models import Note
    user, first = bootstrap_identity(email='retained-counts@example.test', organization='retained-a', name='A', password='Str0ng-schema-fixture!79')
    _, second = bootstrap_identity(email=user.email, organization='retained-b', name='B')
    with tenant_context(organization_id=first.pk, actor=user, source='test:retained'):
        Note(organization=first, owner=user, code='a', title='A').save()
    with tenant_context(organization_id=second.pk, actor=user, source='test:retained'):
        Note(organization=second, owner=user, code='b', title='B').save()
    snapshot = export_schema('example_notes')
    assert snapshot['impact']['organizations'] == 2
    assert snapshot['impact']['records'] == 2


def test_retained_schema_checks_and_repairs_exact_guards_without_public_model(tmp_path, monkeypatch):
    from skeleton.system import checks, synchronization
    from skeleton.system.registry import all_specs
    snapshot = export_schema('example_notes')
    source = tmp_path / 'retained.json'
    raw = json.dumps(snapshot, sort_keys=True).encode()
    source.write_bytes(raw)
    frozen = tmp_path / 'infrastructure.json'
    frozen_raw = json.dumps(export_inventory(), sort_keys=True).encode()
    frozen.write_bytes(frozen_raw)
    installed = tuple(spec for spec in all_specs() if spec.model._meta.app_label != 'example_notes')
    monkeypatch.setattr(checks, 'all_specs', lambda: installed)
    monkeypatch.setattr(synchronization, 'all_specs', lambda: installed)
    with override_settings(
        SKELETON_RETAINED_SCHEMA_PATHS=[{'path': str(source), 'sha256': hashlib.sha256(raw).hexdigest()}],
        SKELETON_FROZEN_INFRASTRUCTURE={'path': str(frozen), 'sha256': hashlib.sha256(frozen_raw).hexdigest()},
    ):
        call_command('sync_table_catalog', verbosity=0)
        call_command('check_skeleton_schema', verbosity=0)
        table = snapshot['tables'][0]['db_table']
        if connection.vendor == 'postgresql':
            import psycopg
            from psycopg import sql
            from tools.integration_database import disposable_database_pair
            pair = disposable_database_pair()
            with psycopg.connect(pair.owner, autocommit=True) as owner:
                owner.execute(sql.SQL('DROP TRIGGER {} ON skeleton.{}').format(
                    sql.Identifier(table + '_sk_guard'), sql.Identifier(table)))
        else:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP TRIGGER {connection.ops.quote_name(table + "_sk_guard_update")}')
        with pytest.raises(CommandError):
            call_command('check_skeleton_schema', verbosity=0)
        if connection.vendor == 'postgresql':
            import environ
            from django.db import connections
            from django.db.utils import ConnectionHandler
            config = environ.Env.db_url_config(pair.owner)
            config['OPTIONS'] = {'options': '-c search_path=skeleton,public'}
            alias = 'retained_schema_owner'
            connections.databases[alias] = ConnectionHandler({'default': config}).settings['default']
            try:
                call_command('reinstall_record_infrastructure', database=alias, verbosity=0)
            finally:
                connections[alias].close()
                del connections[alias]
                del connections.databases[alias]
        else:
            call_command('reinstall_record_infrastructure', verbosity=0)
        call_command('check_skeleton_schema', verbosity=0)
        source.write_bytes(b'{}')
        with pytest.raises(CommandError):
            call_command('check_skeleton_schema', verbosity=0)
