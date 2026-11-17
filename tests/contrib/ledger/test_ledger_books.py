"""Books of the ledger (#542): one default per organization, immutable currency and basis, authorization."""

import pytest
from django.db import IntegrityError, connection, transaction
from django.urls import reverse

from skeleton.contrib.ledger.models import Book
from skeleton.contrib.ledger.service_api import LedgerService
from skeleton.service_api import Forbidden, InvalidInput, NotFound
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
)


def test_the_service_keeps_its_contract():
    assert_service_contract(LedgerService)


def test_one_default_book_per_organization(scenario, book):
    again = scenario.caller(lambda actor: LedgerService().create_default_book(actor=actor, basis="accrual"))()
    assert again.sysid == book.sysid and again.basis == "cash" and again.currency == "BRL" and again.writable
    listed = scenario.caller(lambda actor: LedgerService().books(actor=actor))()
    assert [item.sysid for item in listed] == [book.sysid]


def test_a_concurrent_first_visit_reads_the_winner(scenario, book, monkeypatch):
    from skeleton.contrib.ledger import services

    real = Book.objects.filter
    calls = []

    def stale(*args, **kwargs):
        calls.append(kwargs)
        if kwargs == {"is_default": True} and len(calls) == 1:
            return Book.objects.none()
        return real(*args, **kwargs)

    monkeypatch.setattr(services.Book.objects, "filter", stale)
    with acting_as(scenario.member, scenario.a) as actor:
        assert services.ensure_default_book(actor).pk == book.sysid
    monkeypatch.undo()
    with acting_as(scenario.member, scenario.a):
        assert Book.objects.filter(is_default=True).count() == 1


def test_currency_and_basis_are_immutable_in_the_database(scenario, book):
    with acting_as(scenario.member, scenario.a):
        for assignment in ("currency='USD'", "basis='accrual'"):
            with pytest.raises(IntegrityError, match="immutable record identity"), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE skeleton.skeleton_ledger_book SET {assignment}, revision=revision+1 WHERE sysid=%s",
                        [book.sysid],
                    )


def test_an_unsupported_currency_or_basis_is_refused(scenario):
    with pytest.raises(InvalidInput):
        scenario.caller(lambda actor: LedgerService().create_default_book(actor=actor, currency="XYZ"))()
    with pytest.raises(InvalidInput):
        scenario.caller(lambda actor: LedgerService().create_default_book(actor=actor, basis="mixed"))()


def test_default_deny_isolation_disabled_app_and_actor(scenario, book):
    with pytest.raises(Forbidden):
        scenario.caller(lambda actor: LedgerService().books(actor=actor), user=scenario.denied)()
    with pytest.raises(Forbidden):
        scenario.caller(lambda actor: LedgerService().create_default_book(actor=actor), user=scenario.denied)()
    foreign = scenario.caller(lambda actor: LedgerService().create_default_book(actor=actor), organization=scenario.b)()
    assert_tenant_isolated(scenario.caller(lambda actor: LedgerService().books(actor=actor)), foreign=[foreign.sysid])
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: LedgerService().books(actor=actor)), disabled=scenario.application_disabled
    )
    assert_rejects_other_actor(scenario.caller(lambda actor: LedgerService().books(actor=scenario.admin_a)))


def test_a_private_default_book_of_someone_else_is_not_found(scenario):
    scenario.caller(lambda actor: LedgerService().create_default_book(actor=actor), user=scenario.admin_a)()
    with pytest.raises(NotFound):
        scenario.caller(lambda actor: LedgerService().create_default_book(actor=actor))()


def test_the_page_is_404_without_read_permission(scenario, book):
    from django.test import Client

    client = Client()
    client.force_login(scenario.denied)
    assert client.get(reverse("skeleton_ledger:home", kwargs={"organization_id": scenario.a.pk})).status_code == 404
    client.force_login(scenario.member)
    page = client.get(reverse("skeleton_ledger:home", kwargs={"organization_id": scenario.a.pk}))
    assert page.status_code == 200 and "Livro principal" in page.content.decode()


def test_the_deployment_check_covers_the_book_and_reports_its_guard_removed(scenario, owner_schema_editor):
    from skeleton.system.checks.schema import check_deployed_schema

    def book_errors():
        return [error for error in check_deployed_schema() if "skeleton_ledger_book" in error]

    assert book_errors() == []
    with owner_schema_editor() as editor:
        editor.execute('DROP TRIGGER "skeleton_ledger_book_sk_guard" ON skeleton."skeleton_ledger_book"')
    assert book_errors()
