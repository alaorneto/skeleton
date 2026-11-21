"""The deployed-schema check pins the infrastructure of the instance appearance tables (#592)."""

import pytest
from django.db import DatabaseError, connection, transaction

from skeleton.system.checks import check_deployed_schema
from tests.integration_postgres.test_admission_schema import _owner_execute, _rls, _triggers

TABLES = ("sk_appearance_instancetheme", "sk_appearance_instancebrand", "sk_appearance_instancepage")


@pytest.fixture
def pristine(skeleton_db):
    assert connection.vendor == "postgresql", "TEST_DATABASE_URL must select a real PostgreSQL fixture."
    assert check_deployed_schema() == []


def test_appearance_tables_have_their_infrastructure(pristine):
    for table in TABLES:
        assert _rls(table) == (False, False)  # Global: read on every page, before any tenant.
        assert {table + "_sk_guard", table + "_sk_audit"} <= _triggers(table)


def test_the_guard_refuses_a_new_identity_and_a_skipped_revision(pristine):
    from skeleton.appearance.services import create_theme
    from tests.integration.appearance_support import acting, platform_admin

    admin = platform_admin()
    with acting(admin):
        theme = create_theme(actor=admin, data={"name": "Guarda"})
        for statement in (
            "UPDATE skeleton.sk_appearance_instancetheme SET sysid = gen_random_uuid() WHERE sysid = %s",
            "UPDATE skeleton.sk_appearance_instancetheme SET name = 'x' WHERE sysid = %s",
        ):
            with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(statement, [theme.pk])


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("kind", ["guard", "audit"])
def test_missing_trigger_fails_schema_check(pristine, table, kind):
    _owner_execute("DROP TRIGGER {} ON skeleton.{}", f"{table}_sk_{kind}", table)
    assert any(table in error for error in check_deployed_schema())


def test_package_columns_and_constraints(pristine, subtests):
    """#635: a form theme has no package fields, and one live row per package id (model or database)."""
    from django.core.exceptions import ValidationError

    REFUSED = (DatabaseError, ValidationError)
    from skeleton.appearance.models import InstanceTheme
    from tests.integration.appearance_support import acting, platform_admin

    admin = platform_admin()
    with acting(admin):
        with subtests.test(case="form theme with a package id"):
            with pytest.raises(REFUSED), transaction.atomic():
                InstanceTheme.objects.create(name="Forma", package_id="org.x")
        with subtests.test(case="unknown source"):
            with pytest.raises(REFUSED), transaction.atomic():
                InstanceTheme.objects.create(name="Fonte", source="zip")
        with subtests.test(case="two live rows of one package"):
            InstanceTheme.objects.create(name="Um", source="package", package_id="org.x")
            with pytest.raises(REFUSED), transaction.atomic():
                InstanceTheme.objects.create(name="Dois", source="package", package_id="org.x")
    assert check_deployed_schema() == []


def test_one_kind_of_active_theme_at_a_time(pristine, subtests):
    """#643: builtin_theme, active_theme and app_theme never two at once (model or database)."""
    from django.core.exceptions import ValidationError

    from skeleton.appearance.models import InstanceBrand, InstanceTheme
    from tests.integration.appearance_support import acting, platform_admin

    admin = platform_admin()
    with acting(admin):
        theme = InstanceTheme.objects.create(name="Um")
        for case, values in (
            ("builtin and app", {"builtin_theme": "vinho", "app_theme": "x:y"}),
            ("catalog and app", {"active_theme": theme, "app_theme": "x:y"}),
        ):
            with subtests.test(case=case):
                with pytest.raises((DatabaseError, ValidationError)), transaction.atomic():
                    InstanceBrand.objects.create(**values)
        InstanceBrand.objects.create(app_theme="x:y")
    assert check_deployed_schema() == []
