"""Authorization is not repeated per table or per row in the dashboard, catalog and lists (#302).

Every comparison runs on PostgreSQL with the runtime role. ``readable_specs`` must give the same
answer as ``authorized_queryset`` per table; the budgets prove the work no longer grows with the
number of tables or rows. Nothing is cached between requests.
"""

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests.support import login

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
TABLE = "example_notes.note"


@pytest.fixture
def scenario():
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="query-budget")


def old_readable(user):
    """The previous loop: a whole authorization per exposed table."""
    from skeleton.access.policies import authorized_queryset
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import all_specs

    result = []
    for spec in all_specs():
        if not spec.expose_api or not issubclass(spec.model, SecuredRecord):
            continue
        try:
            authorized_queryset(spec.model, user=user)
        except PermissionDenied:
            continue
        result.append(spec)
    return result


def labels(specs):
    return sorted(spec.model._meta.label_lower for spec in specs)


def test_readable_specs_matches_authorized_queryset(scenario, subtests):
    from skeleton.records.services import readable_specs
    from skeleton.system.models import TableDefinition
    from skeleton.tenancy.context import system_context

    def compare(user, organization):
        with acting_as(user, organization) as actor:
            return labels(readable_specs(actor)), labels(old_readable(actor))

    for name in ("admin_a", "member", "denied"):
        with subtests.test(name):
            new, old = compare(getattr(scenario, name), scenario.a)
            assert new == old
    with subtests.test("member in B"):
        new, old = compare(scenario.member, scenario.b)
        assert new == old and TABLE in new
    with subtests.test("app disabled"), scenario.application_disabled():
        new, old = compare(scenario.member, scenario.a)
        assert new == old and TABLE not in new
    with subtests.test("table unavailable"):
        with system_context(reason="test:table-unavailable"):
            TableDefinition.objects.filter(model_label=TABLE).update(available=False)
        try:
            new, old = compare(scenario.member, scenario.a)
            assert new == old and TABLE not in new
        finally:
            with system_context(reason="test:table-available"):
                TableDefinition.objects.filter(model_label=TABLE).update(available=True)


def _count(call):
    with CaptureQueriesContext(connection) as captured:
        call()
    return len(captured)


def test_readable_specs_queries_do_not_grow_with_tables(scenario, monkeypatch):
    """Five candidate tables cost the same as one (the old loop paid an authorization each)."""
    from skeleton.records import services
    from skeleton.system.registry import get_spec

    spec = get_spec(TABLE)
    with acting_as(scenario.member, scenario.a) as actor:
        monkeypatch.setattr(services, "all_specs", lambda: [spec])
        services.readable_specs(actor)  # Warm the per-request caches of the context.
        one = _count(lambda: services.readable_specs(actor))
        monkeypatch.setattr(services, "all_specs", lambda: [spec] * 5)
        five = _count(lambda: services.readable_specs(actor))
        old = _count(lambda: [old_readable(actor) for _ in range(1)])
    assert one == five
    assert one <= old


def test_dashboard_and_catalog_queries_do_not_grow_with_tables(scenario, monkeypatch, subtests):
    from skeleton.records import services
    from skeleton.system.registry import get_spec

    spec = get_spec(TABLE)
    client = login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
    org = {"organization_id": scenario.a.pk}
    for url in (
        reverse("web:dashboard", kwargs=org),
        reverse("web:catalog", kwargs=org),
        f"/api/v1/o/{scenario.a.pk}/catalog/",
    ):
        with subtests.test(url):
            monkeypatch.setattr(services, "all_specs", lambda: [spec])
            client.get(url)  # Warm the per-process caches (translations, templates).
            one = _count(lambda url=url: client.get(url))
            monkeypatch.setattr(services, "all_specs", lambda: [spec] * 4)
            four = _count(lambda url=url: client.get(url))
            assert one == four


def test_access_list_queries_do_not_grow_with_rows(scenario):
    from skeleton.access.models import AccessGroup, GroupMember
    from skeleton.access.services import provision_membership
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.context import system_context

    client = login(scenario.admin_a, password=FIXTURE_PASSWORD, csrf=False)
    url = reverse("web:access-list", kwargs={"organization_id": scenario.a.pk, "resource": "group-members"})

    def add(start, count):
        from skeleton.access.models import Role

        with system_context(reason="test:access-list-rows", organization_id=scenario.a.pk):
            group, _ = AccessGroup.objects.get_or_create(organization=scenario.a, code="budget", name="Budget")
            role = Role.objects.get(code="member")
            for index in range(start, start + count):
                user = create_identity(email=f"row-{index}@query-budget.example.test", password=None)
                membership = provision_membership(organization=scenario.a, user=user, role=role)
                GroupMember(organization=scenario.a, group=group, membership=membership).save()

    add(0, 2)
    client.get(url)
    few = _count(lambda: client.get(url))
    add(2, 8)
    assert _count(lambda: client.get(url)) == few


def test_record_detail_actions_unchanged(scenario, subtests):
    from skeleton.access.models import RecordGrant
    from skeleton.system.models import TableDefinition
    from skeleton.tenancy.context import system_context

    note = scenario.create(scenario.a, owner=scenario.member, code="BUD-1", title="Budget", visibility="organization")
    with system_context(reason="test:record-grant", organization_id=scenario.a.pk):
        from skeleton.tenancy.models import Membership

        denied_membership = Membership.objects.get(organization=scenario.a, user=scenario.denied)
        table = TableDefinition.objects.get(model_label=TABLE)
        RecordGrant(
            organization=scenario.a, table=table, record_sysid=note.pk, membership=denied_membership, action="update"
        ).save()
    url = reverse("web:record", kwargs={"organization_id": scenario.a.pk, "table_key": TABLE, "sysid": note.pk})
    labels_of = {"update": "Editar", "delete": "Remover", "manage_access": "Gerenciar acesso"}
    for name in ("member", "admin_a", "denied"):
        with subtests.test(name):
            user = getattr(scenario, name)
            with acting_as(user, scenario.a) as actor:
                old = old_permissions(note, actor)
            response = login(user, password=FIXTURE_PASSWORD, csrf=False).get(url)
            if old is None:  # Not readable at all: no page and no actions, before and after.
                assert response.status_code in (403, 404)
                continue
            expected = {labels_of[action] for action, allowed in old.items() if allowed}
            assert {str(action["label"]) for action in response.context["actions"]} == expected


def old_permissions(note, actor):
    """The previous detail logic: an authorized queryset per action, then the record check."""
    from skeleton.access.policies import require_record_action
    from skeleton.records.services import exposed_spec, record_queryset, require_access_management

    spec = exposed_spec(TABLE)
    try:
        record = record_queryset(spec, user=actor).filter(pk=note.pk).first()
    except PermissionDenied:
        return None
    if record is None:
        return None
    result = {}
    for action in ("update", "delete"):
        try:
            record_queryset(spec, user=actor, action=action)
            require_record_action(record, user=actor, action=action)
            result[action] = True
        except PermissionDenied:
            result[action] = False
    try:
        require_access_management(record, user=actor)
        result["manage_access"] = True
    except PermissionDenied:
        result["manage_access"] = False
    return result


def test_revoked_role_disappears_on_next_request(scenario):
    """Negative: nothing about the tables is cached between requests."""
    from skeleton.access.models import MemberRole, Role
    from skeleton.access.services import revoke_link

    client = login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
    url = reverse("web:catalog", kwargs={"organization_id": scenario.a.pk})
    assert TABLE in client.get(url).content.decode()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        contract = Role.objects.get(code="service-contract")
        link = MemberRole.objects.get(role=contract, membership__user=scenario.member)
        revoke_link(link, user=admin)
    assert TABLE not in client.get(url).content.decode()
