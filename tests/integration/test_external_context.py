"""External access (#531): the portal context, its validation and the explicit denials of member powers.

Runs on PostgreSQL with the runtime role.
"""

import importlib
import inspect
from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection

from skeleton.tenancy.context import InvalidContext, external_context, system_context, tenant_context
from skeleton.testing.external import acting_external, enable_portal, external_scenario, grant_external_access

pytestmark = pytest.mark.usefixtures("skeleton_db")
UNAVAILABLE = "Organization is unavailable."


@pytest.fixture
def scenario():
    return external_scenario("ext-context")


def guc(name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [f"skeleton.{name}"])
        return cursor.fetchone()[0]


def test_an_external_person_opens_the_portal_and_never_the_organization(scenario):
    with pytest.raises(PermissionDenied, match=UNAVAILABLE):
        with tenant_context(organization_id=scenario.a.pk, actor=scenario.external, source="http"):
            pass
    with acting_external(scenario.external, scenario.a):
        from skeleton.tenancy.context import current_context

        context = current_context()
        assert context.external and context.organization_id == scenario.a.pk and not context.platform_admin
        assert guc("external") == "on" and guc("source") == "http:portal"
    with tenant_context(organization_id=scenario.a.pk, actor=scenario.admin_a, source="http"):
        assert guc("external") == "off"


def test_every_missing_condition_answers_the_same(scenario):
    from skeleton.accounts.models import User
    from skeleton.tenancy.models import ExternalAccess, Organization

    def refused(user, organization):
        with pytest.raises(PermissionDenied, match=UNAVAILABLE):
            with external_context(organization_id=organization.pk, actor=user):
                pass

    refused(scenario.outsider, scenario.a)  # No access.
    refused(scenario.external, scenario.b)  # Access in A never opens B.
    enable_portal(scenario.a, enabled=False)
    refused(scenario.external, scenario.a)  # Portal off.
    enable_portal(scenario.a)
    with system_context(reason="test:external-deactivate", organization_id=scenario.a.pk):
        access = ExternalAccess.objects.get(user=scenario.external)
        access.is_active = False
        access.save(update_fields={"is_active"})
    refused(scenario.external, scenario.a)  # Inactive access.
    with system_context(reason="test:external-deactivate", organization_id=scenario.a.pk):
        access.is_active = True
        access.save(update_fields={"is_active"})
    with system_context(reason="test:external-user"):
        User.objects.filter(pk=scenario.external.pk).update(is_active=False)
    refused(scenario.external, scenario.a)  # Inactive person.
    with system_context(reason="test:external-user"):
        User.objects.filter(pk=scenario.external.pk).update(is_active=True)
    with acting_external(scenario.external, scenario.a):
        pass
    with system_context(reason="test:external-org"):
        organization = Organization.objects.get(pk=scenario.a.pk)
        organization.is_active = False
        organization.save(update_fields={"is_active"})
    refused(scenario.external, scenario.a)  # Inactive organization.


def test_contexts_never_nest(scenario):
    with acting_external(scenario.dual, scenario.a), pytest.raises(InvalidContext):
        with tenant_context(organization_id=scenario.a.pk, actor=scenario.dual, source="http"):
            pass
    with (
        tenant_context(organization_id=scenario.a.pk, actor=scenario.dual, source="http"),
        pytest.raises(InvalidContext),
    ):
        with external_context(organization_id=scenario.a.pk, actor=scenario.dual):
            pass
    with acting_external(scenario.dual, scenario.a), pytest.raises(InvalidContext):
        with system_context(reason="test:promote", organization_id=scenario.a.pk):
            pass


@pytest.mark.needs_example_app
def test_member_powers_are_denied_even_to_the_administrator_who_is_also_external(scenario):
    from examples.notes.models import Note
    from skeleton.access import capabilities, policies

    with tenant_context(organization_id=scenario.a.pk, actor=scenario.dual, source="http"):
        assert policies.is_organization_admin(scenario.dual)  # A member context keeps the powers.
        assert capabilities.capabilities_for(scenario.dual)
    with acting_external(scenario.dual, scenario.a):
        user = scenario.dual
        assert policies.is_organization_admin(user) is False
        assert policies.has_table_permission(Note, user=user, action="read") is False
        assert policies.table_permissions(user) == frozenset()
        assert list(policies.effective_roles(user)) == []
        assert list(policies.group_ids(user)) == []
        assert capabilities.capabilities_for(user) == frozenset()
        assert capabilities.has_capability(user, capabilities.EXTERNAL_MANAGE) is False
        for denied in (
            lambda: policies.active_membership(user),
            lambda: policies.authorized_queryset(Note, user=user),
            lambda: policies.require_record_action(Note(organization_id=scenario.a.pk), user=user, action="read"),
            lambda: policies.authorized_audit_queryset(user),
        ):
            with pytest.raises(PermissionDenied):
                denied()


@pytest.mark.needs_example_app
def test_the_record_gateway_refuses_the_external_context(scenario):
    from skeleton.records import references, services
    from skeleton.system.registry import get_spec

    spec = get_spec("example_notes.note")
    with acting_external(scenario.dual, scenario.a):
        user = scenario.dual
        for denied in (
            lambda: services.exposed_spec("example_notes.note"),
            lambda: services.readable_specs(user),
            lambda: services.record_queryset(spec, user=user),
            lambda: services.create_record(spec, user=user, data={"title": "x"}),
            lambda: references.reference_queryset(spec, "owner", user=user),
        ):
            with pytest.raises(PermissionDenied):
                denied()


def test_service_methods_refuse_the_external_context_unless_marked(scenario):
    from skeleton.service_api import Forbidden
    from skeleton.service_api.base import ServiceAPI, service_method
    from skeleton.service_api.organizations import OrganizationService

    class Portal(ServiceAPI):
        @service_method(read_only=True, since="0.6.0", allow_external=True)
        def hello(self, *, actor) -> str:
            """Say hello.

            Autorização: pessoa externa.
            Erros: nenhum.
            Efeitos: nenhum.
            """
            return "olá"

    with acting_external(scenario.external, scenario.a):
        assert Portal().hello(actor=scenario.external) == "olá"
        with pytest.raises(Forbidden) as error:
            OrganizationService().current(actor=scenario.external)
        assert error.value.code == "external_not_allowed"
    assert Portal.service_methods()["hello"].allow_external is True


def test_only_the_expected_core_service_methods_are_open_to_external_people():
    from skeleton.service_api.base import ServiceAPI

    marked = []
    root = Path(__file__).resolve().parents[2] / "skeleton" / "service_api"
    for path in sorted(root.glob("*.py")):
        module = importlib.import_module(f"skeleton.service_api.{path.stem}")
        for name, value in inspect.getmembers(module, inspect.isclass):
            if issubclass(value, ServiceAPI) and value is not ServiceAPI and value.__module__ == module.__name__:
                marked += [
                    f"{name}.{method}" for method, spec in value.service_methods().items() if spec.allow_external
                ]
    assert marked == ["ExternalAccessService.me"]  # The person's own access (#532).


def test_self_discovery_lists_only_the_persons_own_accesses(scenario):
    from skeleton.tenancy.context import own_rows, request_context
    from skeleton.tenancy.models import ExternalAccess

    grant_external_access(scenario.b, scenario.external)
    with request_context(actor=scenario.external, request_id=None, source="http"):
        assert set(own_rows(ExternalAccess, scenario.external).values_list("organization_id", flat=True)) == {
            scenario.a.pk,
            scenario.b.pk,
        }
    with request_context(actor=scenario.outsider, request_id=None, source="http"):
        assert not own_rows(ExternalAccess, scenario.outsider).exists()
        assert not ExternalAccess.all_objects.filter(user=scenario.external).exists()  # RLS: not the actor's rows.


def test_a_write_in_the_portal_is_audited_with_the_person_and_the_portal_source(scenario):
    from django.utils import timezone

    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.models import ExternalAccess

    with acting_external(scenario.external, scenario.a):
        access = ExternalAccess.objects.get(user=scenario.external)
        access.last_seen_at = timezone.now()
        access.save(update_fields={"last_seen_at"})
    with tenant_context(organization_id=scenario.a.pk, actor=scenario.admin_a, source="http"):
        event = AuditEvent.objects.filter(record_sysid=access.pk, operation="update").latest("created_at")
    assert (event.actor_sysid, event.source) == (scenario.external.pk, "http:portal")


def test_seats_never_count_external_people(scenario):
    from skeleton.billing.quota import _external_people, _members
    from skeleton.tenancy.models import ExternalAccess

    with system_context(reason="test:external-metrics", organization_id=scenario.a.pk):
        assert _members(scenario.a.pk) == 3  # The owner, the dual administrator and the member.
        assert _external_people(scenario.a.pk) == 2
        access = ExternalAccess.objects.get(user=scenario.external)
        access.is_active = False
        access.save(update_fields={"is_active"})
        assert _external_people(scenario.a.pk) == 1


def test_the_deployment_check_covers_the_new_tables(owner_schema_editor):
    from skeleton.system.checks.schema import check_deployed_schema

    def errors():
        return [error for error in check_deployed_schema() if "sk_tenancy_externalaccess" in error]

    assert errors() == []
    with owner_schema_editor() as editor:
        editor.execute('DROP TRIGGER "sk_tenancy_externalaccess_sk_guard" ON skeleton."sk_tenancy_externalaccess"')
    assert errors()
