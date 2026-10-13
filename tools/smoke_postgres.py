"""Reset a disposable schema, migrate, seed, then assert security using runtime.

Required: TEST_DATABASE_URL (skeleton_runtime), TEST_MIGRATION_DATABASE_URL
(skeleton_owner), SKELETON_TEST_ALLOW_SCHEMA_RESET=1; both DSNs must name the
same pre-provisioned *_skeleton_test database, distinct from application DSNs.
No database/role provisioning is performed; secrets are never printed.
"""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from tools.integration_database import disposable_database_pair, reset_schema
    pair = disposable_database_pair()
    reset_schema(pair)
    env = dict(os.environ, DJANGO_SETTINGS_MODULE='config.settings.migrate',
        MIGRATION_DATABASE_URL=pair.owner, SKELETON_ENABLE_EXAMPLE_APP='true')
    subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'migrate', '--noinput'],
        cwd=ROOT, env=env, check=True)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.test'
    os.environ['SKELETON_ENABLE_EXAMPLE_APP'] = 'true'
    import django
    django.setup()
    from django.db import connection
    if connection.vendor != 'postgresql':
        raise RuntimeError('Smoke requires actual PostgreSQL; SQLite is not an RLS substitute.')
    from tools.security_fixtures import build_security_fixture
    from tools.postgres_assertions import assert_runtime_security
    fixture = build_security_fixture()
    assert_runtime_security(pair, fixture, emit=print)
    from django.core.management import call_command
    call_command('check_skeleton_schema', database='default')
    print('PostgreSQL runtime security smoke passed; disposable schema remains for inspection.')


if __name__ == '__main__':
    main()
