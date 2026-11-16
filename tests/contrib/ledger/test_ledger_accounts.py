"""Chart of accounts, account keys and cost centers (#543)."""

import pytest
from django.db import IntegrityError, connection, transaction

from skeleton.contrib.ledger.registry import AccountKey, register_account_key
from skeleton.contrib.ledger.service_api import LedgerService
from skeleton.service_api import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.testing.service_api import acting_as, assert_tenant_isolated, assert_unavailable_when_disabled


def call(scenario, method, user=None, organization=None, **kwargs):
    return scenario.caller(
        lambda actor: getattr(LedgerService(), method)(actor=actor, **kwargs), user=user, organization=organization
    )()


def chart(scenario):
    call(scenario, "apply_chart")
    return {item.code: item for item in call(scenario, "accounts", page_size=100).items}


def test_the_template_creates_accounts_and_binds_every_base_key_once(scenario, book):
    accounts = chart(scenario)
    assert {"1", "1.1", "4.1", "5.4"} <= set(accounts) and accounts["1"].is_group and not accounts["1.1"].is_group
    keys = {item.code: item for item in call(scenario, "account_keys")}
    base = [code for code in keys if code.startswith("ledger.")]
    assert len(base) == 12 and all(keys[code].account_id for code in base)
    assert call(scenario, "apply_chart") == 0  # Idempotent.


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"code": "9", "kind": "asset", "parent": "1.1"}, "parent"),
        ({"code": "9", "kind": "revenue", "parent": "1"}, "parent"),
        ({"code": "9", "kind": "bogus"}, "kind"),
    ],
)
def test_invalid_accounts_are_refused_by_the_model(scenario, book, changes, field):
    accounts = chart(scenario)
    parent = accounts[changes["parent"]].sysid if "parent" in changes else None
    with pytest.raises(InvalidInput) as error:
        call(scenario, "create_account", code=changes["code"], name="X", kind=changes["kind"], parent_id=parent)
    assert field in error.value.errors or "__all__" in error.value.errors


def test_a_repeated_code_is_an_integrity_conflict(scenario, book):
    chart(scenario)
    with pytest.raises(Conflict) as error:
        call(scenario, "create_account", code="1.1", name="X", kind="asset")
    assert error.value.code == "integrity_conflict"


def test_the_database_refuses_a_parent_of_another_book_or_kind(scenario, book):
    accounts = chart(scenario)
    with acting_as(scenario.member, scenario.a), connection.cursor() as cursor:
        for kind in ("revenue",):
            with pytest.raises(IntegrityError), transaction.atomic():
                cursor.execute(
                    "INSERT INTO skeleton.skeleton_ledger_account (sysid, created_at, updated_at, revision, organization_id,"
                    " book_id, code, name, kind, parent_id, is_group, is_active) VALUES"
                    " (gen_random_uuid(), now(), now(), 1, %s, %s, 'X9', 'x', %s, %s, false, true)",
                    [scenario.a.pk, book.sysid, kind, accounts["1"].sysid],
                )
        with pytest.raises(IntegrityError, match="immutable record identity"), transaction.atomic():
            cursor.execute(
                "UPDATE skeleton.skeleton_ledger_account SET code='Z', revision=revision+1 WHERE sysid=%s",
                [accounts["1.1"].sysid],
            )


def test_name_and_activity_change_with_cas(scenario, book):
    account = chart(scenario)["1.1"]
    changed = call(
        scenario, "update_account", account_id=account.sysid, expected_revision=account.revision, name="Caixa loja"
    )
    assert changed.name == "Caixa loja" and changed.revision == account.revision + 1
    with pytest.raises(Conflict) as stale:
        call(scenario, "update_account", account_id=account.sysid, expected_revision=account.revision, is_active=False)
    assert stale.value.code == "stale_revision"


def test_binding_rules(scenario, book):
    accounts = chart(scenario)
    for account in ("1", "4.1"):  # A group, and an account of another kind.
        with pytest.raises(InvalidInput):
            call(scenario, "bind_key", key="ledger.cash", account_id=accounts[account].sysid)
    with pytest.raises(InvalidInput):
        call(scenario, "bind_key", key="ledger.unknown", account_id=accounts["1.2"].sysid)
    bound = call(scenario, "bind_key", key="ledger.cash", account_id=accounts["1.2"].sysid)
    assert bound.account_id == accounts["1.2"].sysid
    inactive = accounts["1.3"]
    call(scenario, "update_account", account_id=inactive.sysid, expected_revision=inactive.revision, is_active=False)
    with pytest.raises(InvalidInput):
        call(scenario, "bind_key", key="ledger.cash", account_id=inactive.sysid)


def test_keys_of_an_app_appear_only_while_it_is_active(scenario, book):
    from skeleton.testing.applications import activate_app, open_app

    register_account_key(AccountKey(code="example_notes.fees_received", label="Honorários", kind="revenue"))
    register_account_key(AccountKey(code="acme_absent.fees", label="Ausente", kind="revenue"))
    assert "acme_absent.fees" not in {item.code for item in call(scenario, "account_keys")}
    open_app("example_notes")
    activate_app("example_notes", scenario.a.pk)
    labels = {item.code: item.label for item in call(scenario, "account_keys")}
    assert labels["example_notes.fees_received"] == "Honorários"


def test_only_who_manages_the_book_changes_the_chart(scenario, book):
    from skeleton.access.models import Action, TablePermission
    from skeleton.tenancy.context import system_context

    call(scenario, "apply_chart")
    with system_context(reason="fixture:ledger-read-only", organization_id=scenario.a.pk):
        TablePermission.objects.filter(table__model_label="skeleton_ledger.book", action=Action.UPDATE).delete()
    with pytest.raises(Forbidden):
        call(scenario, "create_account", code="9", name="x", kind="asset")
    assert call(scenario, "accounts").items  # Reading remains.
    with pytest.raises(NotFound):
        call(scenario, "accounts", user=scenario.denied)


def test_an_account_with_children_or_bindings_is_not_deleted(scenario, book):
    accounts = chart(scenario)
    for code in ("1", "1.1"):
        with pytest.raises(Conflict) as error:
            call(scenario, "delete_account", account_id=accounts[code].sysid, expected_revision=accounts[code].revision)
        assert error.value.code == "live_references"
    free = call(scenario, "create_account", code="6", name="Livre", kind="asset")
    call(scenario, "delete_account", account_id=free.sysid, expected_revision=free.revision)
    assert "6" not in {item.code for item in call(scenario, "accounts", page_size=100).items}


def test_cost_centers_isolation_and_disabled_app(scenario, book):
    center = call(scenario, "create_cost_center", code="CC1", name="Unidade 1")
    assert [item.sysid for item in call(scenario, "cost_centers")] == [center.sysid]
    with pytest.raises(Conflict):
        call(scenario, "create_cost_center", code="CC1", name="Repetido")
    call(scenario, "create_default_book", organization=scenario.b)
    foreign = call(scenario, "create_cost_center", organization=scenario.b, code="B1", name="B")
    assert_tenant_isolated(
        scenario.caller(lambda actor: LedgerService().cost_centers(actor=actor)), foreign=[foreign.sysid]
    )
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: LedgerService().accounts(actor=actor)), disabled=scenario.application_disabled
    )
