"""The deployed-schema check pins the search infrastructure (#180)."""

import pytest

from skeleton.system.checks import check_deployed_schema
from tools.integration_database import disposable_database_pair


def _owner_execute(statement):
    import psycopg

    pair = disposable_database_pair()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(statement)


@pytest.fixture
def pristine(skeleton_db):
    from django.apps import apps
    from django.db import connection

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    assert connection.vendor == "postgresql"
    assert check_deployed_schema() == []


def test_missing_search_index_fails_the_schema_check(pristine):
    _owner_execute("DROP INDEX skeleton.example_notes_note_sk_search")
    assert any("example_notes_note: search index" in error for error in check_deployed_schema())


def test_missing_unaccent_function_fails_the_schema_check(pristine):
    _owner_execute("DROP FUNCTION skeleton.immutable_unaccent(text) CASCADE")
    assert any("immutable_unaccent" in error for error in check_deployed_schema())
