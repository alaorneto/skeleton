"""Pages of the ledger (#549): through LedgerService only, localized amounts, CAS, 400 and 404, no scripts."""

import datetime as dt
import re
import uuid
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.contrib.ledger.service_api import LedgerService

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)", re.IGNORECASE)


def call(scenario, operation, organization=None, **kwargs):
    return scenario.caller(
        lambda actor: getattr(LedgerService(), operation)(actor=actor, **kwargs), organization=organization
    )()


def url(scenario, name, *args, organization=None):
    return reverse(f"skeleton_ledger:{name}", args=((organization or scenario.a).pk, *args))


@pytest.fixture
def ready(scenario):
    call(scenario, "create_default_book")
    call(scenario, "apply_chart")
    return {item.code: item for item in call(scenario, "accounts", page_size=100).items}


@pytest.fixture
def client(scenario):
    client = Client()
    client.force_login(scenario.member)
    return client


def test_the_pages_read_and_write_only_through_the_service():
    source = (Path(__file__).parents[3] / "skeleton/contrib/ledger/views.py").read_text(encoding="utf-8")
    assert (
        "from .models" not in source
        and ".objects" not in source
        and "services import" not in source.replace("from .services import grant_member_access", "")
    )


def test_every_page_renders_without_inline_scripts(scenario, ready, client):
    title = call(
        scenario,
        "create_title",
        direction="receivable",
        amount=123456,
        due_date=dt.date(2027, 5, 10),
        description="Serviço prestado",
        category_key="ledger.revenue",
        idempotency_key="t",
        counterparty_name="Cliente Exemplo",
    )
    pages = [
        url(scenario, "home"),
        url(scenario, "accounts"),
        url(scenario, "entries"),
        url(scenario, "entry-new"),
        url(scenario, "titles"),
        url(scenario, "title-new"),
        url(scenario, "title", title.sysid),
        url(scenario, "recurrences"),
        url(scenario, "reports"),
        *(url(scenario, "report", name) for name in ("trial-balance", "statement", "income", "cash-flow", "aging")),
        url(scenario, "report", "statement") + f"?account={ready['1.1'].sysid}&start=2027-01-01&end=2027-12-31",
    ]
    for page in pages:
        response = client.get(page)
        html = response.content.decode()
        assert response.status_code == 200, page
        assert not INLINE_SCRIPT.search(html), page
        assert "<h1>" in html and 'aria-current="page"' in html, page
    html = client.get(url(scenario, "titles")).content.decode()
    assert "1.234,56" in html and "Cliente Exemplo" in html


@pytest.mark.parametrize(
    ("name", "query"),
    [
        ("entries", "?start=2027-13-01"),
        ("entries", "?start=2027-02-01&end=2027-01-01"),
        ("titles", "?status=lost"),
        ("titles", "?due_until=10/05/2027"),
        ("titles", "?cursor=@@"),
        ("reports", "?start=2020-01-01&end=2027-12-31"),
        ("aging", "?as_of=ontem"),
        ("aging", "?direction=both"),
        ("statement", "?account=8f2d1c55-6a0e-4a4b-9a43-2d4f3e7a1b90"),
    ],
)
def test_invalid_parameters_answer_400(scenario, ready, client, name, query):
    target = url(scenario, "report", name) if name in ("aging", "statement") else url(scenario, name)
    response = client.get(target + query)
    assert response.status_code == 400 and "Pedido inválido" in response.content.decode()


def test_typed_amounts_use_the_decimal_comma_and_refuse_extra_places(scenario, ready, client):
    form = {
        "direction": "payable",
        "description": "Material",
        "counterparty_name": "",
        "category_key": "ledger.expenses",
        "issue_date": "2027-05-01",
        "due_date": "2027-05-10",
        "installments": "3",
        "idempotency_key": "k1",
    }
    refused = client.post(url(scenario, "title-new"), {**form, "amount": "10,123"})
    assert refused.status_code == 400 and "casas decimais" in refused.content.decode()
    created = client.post(url(scenario, "title-new"), {**form, "amount": "1.000,00"})
    assert created.status_code == 302
    parts = call(scenario, "titles", page_size=10).items
    assert [part.amount for part in parts] == [33333, 33333, 33334]


def test_a_manual_entry_and_its_reversal(scenario, ready, client):
    form = {"date": "2027-05-02", "description": "Ajuste", "idempotency_key": "m1"}
    form |= {"account_1": str(ready["1.1"].sysid), "debit_1": "250,00"}
    form |= {"account_2": str(ready["4.1"].sysid), "credit_2": "250"}
    response = client.post(url(scenario, "entry-new"), form)
    assert response.status_code == 302
    [entry] = call(scenario, "entries", start=dt.date(2027, 5, 1), end=dt.date(2027, 5, 31)).items
    page = client.get(url(scenario, "entry", entry.sysid)).content.decode()
    assert "250,00" in page and "Estornar" in page
    unbalanced = client.post(url(scenario, "entry-new"), {**form, "credit_2": "200", "idempotency_key": "m2"})
    assert unbalanced.status_code == 400
    stale = client.post(
        url(scenario, "entry-reverse", entry.sysid), {"revision": "99", "date": "2027-05-03"}, follow=True
    )
    assert "mudou" in stale.content.decode()
    reversed_ = client.post(
        url(scenario, "entry-reverse", entry.sysid), {"revision": str(entry.revision), "date": "2027-05-03"}
    )
    assert reversed_.status_code == 302
    assert call(scenario, "entry", entry_id=entry.sysid).reversed_by_id is not None


def test_settling_with_a_stale_revision_shows_the_conflict(scenario, ready, client):
    title = call(
        scenario,
        "create_title",
        direction="receivable",
        amount=10000,
        due_date=dt.date(2027, 5, 10),
        description="Venda",
        category_key="ledger.revenue",
        idempotency_key="s",
    )
    form = {
        "date": "2027-05-10",
        "payment_account": str(ready["1.2"].sysid),
        "method": "pix",
        "amount": "40,00",
        "discount": "",
        "interest": "",
        "fee": "",
        "idempotency_key": "b1",
        "revision": str(title.revision),
    }
    assert client.post(url(scenario, "title", title.sysid), form).status_code == 302
    stale = client.post(url(scenario, "title", title.sysid), {**form, "idempotency_key": "b2"})
    assert stale.status_code == 400 and "mudou desde que a página abriu" in stale.content.decode()
    current = call(scenario, "title", title_id=title.sysid)
    assert (current.status, current.balance) == ("partial", 6000)
    [settlement] = call(scenario, "settlements", title_id=title.sysid)
    client.post(
        url(scenario, "settlement-reverse", title.sysid, settlement.sysid),
        {"revision": str(current.revision), "date": "2027-05-11"},
    )
    reopened = call(scenario, "title", title_id=title.sysid)
    assert reopened.status == "open"
    client.post(url(scenario, "title-cancel", title.sysid), {"revision": str(reopened.revision)})
    assert call(scenario, "title", title_id=title.sysid).status == "cancelled"


def test_recurrences_lock_and_chart_forms(scenario, ready, client):
    form = {
        "direction": "payable",
        "description": "Aluguel",
        "counterparty_name": "",
        "category_key": "ledger.expenses",
        "frequency": "monthly",
        "interval": "1",
        "day_of_month": "",
        "start_date": "2027-01-31",
        "end_date": "",
        "max_occurrences": "",
        "days_ahead": "10",
        "amount": "2.500,00",
        "idempotency_key": "r1",
    }
    assert client.post(url(scenario, "recurrences"), form).status_code == 302
    [rule] = call(scenario, "recurrences")
    assert rule.amount == 250000
    change = {"description": "Aluguel novo", "end_date": "", "days_ahead": "5", "amount": "2.600,00"}
    assert (
        client.post(url(scenario, "recurrence", rule.sysid), {**change, "revision": str(rule.revision)}).status_code
        == 302
    )
    [changed] = call(scenario, "recurrences")
    assert (changed.amount, changed.description, changed.days_ahead) == (260000, "Aluguel novo", 5)
    client.post(url(scenario, "recurrence-toggle", rule.sysid), {"revision": str(changed.revision), "action": "pause"})
    assert not call(scenario, "recurrences")[0].is_active
    [book] = call(scenario, "books")
    client.post(url(scenario, "lock"), {"revision": str(book.revision), "locked_until": "2027-01-31"})
    assert call(scenario, "books")[0].locked_until == dt.date(2027, 1, 31)
    expense = ready["5.1"]
    client.post(
        url(scenario, "account-active", expense.sysid), {"revision": str(expense.revision), "is_active": "false"}
    )
    accounts = {item.code: item for item in call(scenario, "accounts", page_size=100).items}
    assert not accounts["5.1"].is_active
    created = client.post(
        url(scenario, "accounts"),
        {
            "form": "account",
            "account-code": "5.9",
            "account-name": "Outras",
            "account-kind": "expense",
            "account-parent": str(accounts["5"].sysid),
        },
    )
    assert created.status_code == 302
    duplicate = client.post(
        url(scenario, "accounts"),
        {"form": "account", "account-code": "5.9", "account-name": "Outras", "account-kind": "expense"},
    )
    assert duplicate.status_code == 400
    client.post(url(scenario, "bind-key"), {"key": "ledger.expenses", "account": str(accounts["5.4"].sysid)})
    keys = {key.code: key.account_id for key in call(scenario, "account_keys")}
    assert keys["ledger.expenses"] == accounts["5.4"].sysid


def test_unreadable_records_are_never_rendered(scenario, ready, client):
    call(scenario, "create_default_book", organization=scenario.b)
    call(scenario, "apply_chart", organization=scenario.b)
    other = call(
        scenario,
        "create_title",
        organization=scenario.b,
        direction="receivable",
        amount=500,
        due_date=dt.date(2027, 5, 10),
        description="Título de B",
        category_key="ledger.revenue",
        idempotency_key="b",
    )
    assert client.get(url(scenario, "title", other.sysid)).status_code == 404
    assert client.get(url(scenario, "recurrence", uuid.uuid4())).status_code == 404
    assert client.get(url(scenario, "entry", uuid.uuid4())).status_code == 404
    denied = Client()
    denied.force_login(scenario.denied)
    for name in ("home", "accounts", "titles", "reports"):
        assert denied.get(url(scenario, name)).status_code == 404, name
