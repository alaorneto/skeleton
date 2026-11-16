"""Pages of the ledger (#542, #549): every read and write goes through ``LedgerService``.

The pages work on the default book of the organization. Amounts are typed and shown in the major
unit of the book's currency (``money.parse`` refuses more decimals than the currency has); dates
and filters in the query string are validated and answer 400 when invalid. Every change carries the
revision it was based on, and a conflict comes back as a readable message.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any
from uuid import UUID as UUIDValue

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_POST

from skeleton.common import money
from skeleton.service_api import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.web.views import tenant_page

from .service_api import LedgerService, PostingLine

if TYPE_CHECKING:
    from uuid import UUID

KINDS = (
    ("asset", gettext_lazy("Ativo")),
    ("liability", gettext_lazy("Passivo")),
    ("equity", gettext_lazy("Patrimônio líquido")),
    ("revenue", gettext_lazy("Receita")),
    ("expense", gettext_lazy("Despesa")),
)
DIRECTIONS = (("receivable", gettext_lazy("A receber")), ("payable", gettext_lazy("A pagar")))
STATUSES = (
    ("open", gettext_lazy("Aberto")),
    ("partial", gettext_lazy("Parcial")),
    ("settled", gettext_lazy("Baixado")),
    ("cancelled", gettext_lazy("Cancelado")),
)
METHODS = (
    ("pix", gettext_lazy("Pix")),
    ("cash", gettext_lazy("Dinheiro")),
    ("card", gettext_lazy("Cartão")),
    ("boleto", gettext_lazy("Boleto")),
    ("transfer", gettext_lazy("Transferência")),
    ("other", gettext_lazy("Outro")),
)
FREQUENCIES = (
    ("monthly", gettext_lazy("Mensal")),
    ("weekly", gettext_lazy("Semanal")),
    ("yearly", gettext_lazy("Anual")),
)
MANUAL_LINES = 6


class BadParameter(Exception):
    pass


def _service() -> LedgerService:
    return LedgerService()


def _conflict_text(error: Conflict) -> str:
    texts = {
        "stale_revision": gettext("O registro mudou desde que a página abriu. Atualize a página e tente de novo."),
        "period_locked": gettext("A data está num período travado do livro."),
        "account_not_bound": gettext("Vincule a chave da conta no plano de contas antes de continuar."),
        "already_reversed": gettext("Já estornado."),
        "title_settled": gettext("O título já está baixado."),
        "title_has_settlements": gettext("Estorne as baixas antes de cancelar o título."),
        "integrity_conflict": gettext("Já existe um registro com esses dados."),
        "live_references": gettext("O registro ainda é usado e não pode ser excluído."),
    }
    return texts.get(error.code, gettext("A operação conflita com o estado atual."))


def _default_book(request: HttpRequest) -> Any:
    try:
        books = _service().books(actor=request.user)
    except (Forbidden, NotFound):
        raise Http404 from None
    return next((book for book in books if book.is_default), None)


def _bad_request(request: HttpRequest, organization_id: UUID, detail: str) -> HttpResponse:
    return render(
        request, "skeleton_ledger/bad_request.html", {"organization_id": organization_id, "detail": detail}, status=400
    )


def _date(value: str | None, default: dt.date | None = None) -> dt.date | None:
    if value is None or value == "":
        return default
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise BadParameter(gettext("Data inválida: use AAAA-MM-DD.")) from None
    if not 1900 <= parsed.year <= 2999:
        raise BadParameter(gettext("Data fora do intervalo aceito."))
    return parsed


def _day(value: str | None, default: dt.date) -> dt.date:
    """A date parameter that falls back to ``default`` when absent."""
    return _date(value, default) or default


def _set_choices(form: forms.Form, name: str, choices: list[tuple[str, str]]) -> None:
    field = form.fields[name]
    if not isinstance(field, forms.ChoiceField):
        raise TypeError(name)
    field.choices = choices


def _choice(value: str | None, choices: Any) -> str | None:
    if value in (None, ""):
        return None
    if value not in {code for code, _ in choices}:
        raise BadParameter(gettext("Filtro desconhecido."))
    return value


def _window(request: HttpRequest) -> tuple[dt.date, dt.date]:
    today = timezone.localdate()
    start = _day(request.GET.get("start"), today.replace(day=1))
    end = _day(request.GET.get("end"), today)
    if end < start:
        raise BadParameter(gettext("O fim vem depois do início."))
    if (end - start).days > 5 * 366:
        raise BadParameter(gettext("Use uma janela de até cinco anos."))
    return start, end


def _amount_text(value: str) -> str:
    """Accept the decimal comma: ``1.234,56`` and ``1234,56`` mean ``1234.56``."""
    text = value.strip().replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    return text


class AmountField(forms.CharField):
    """An amount in the major unit of ``currency``, cleaned to an integer of the minor unit."""

    def __init__(self, *, currency: str, **kwargs: Any) -> None:
        self.currency = currency
        kwargs.setdefault("max_length", 24)
        super().__init__(**kwargs)
        self.widget.attrs.update({"inputmode": "decimal", "autocomplete": "off"})

    def clean(self, value: Any) -> Any:
        text = super().clean(value)
        if text in (None, ""):
            return None if not self.required else 0
        try:
            amount = money.parse(_amount_text(text), self.currency)
        except ValidationError as error:
            raise forms.ValidationError(error.messages) from None
        if amount < 0:
            raise forms.ValidationError(gettext("Use um valor positivo."))
        return amount


def _apply_errors(form: forms.Form, error: InvalidInput) -> None:
    for field, texts in error.errors.items():
        name = field if field in form.fields else None
        for text in texts:
            form.add_error(name, text)


def _revision(request: HttpRequest) -> int:
    try:
        return int(request.POST.get("revision", ""))
    except ValueError:
        raise Http404 from None


def _date_input() -> forms.DateInput:
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


# --------------------------------------------------------------------------- books and lock


@tenant_page()
def home(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    try:
        books = _service().books(actor=request.user)
    except Forbidden:
        raise Http404 from None
    default = next((book for book in books if book.is_default), None)
    return render(
        request,
        "skeleton_ledger/home.html",
        {"organization_id": organization_id, "books": books, "has_default": default is not None, "book": default},
    )


@require_POST
@tenant_page()
@transaction.atomic
def default_book(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    basis = request.POST.get("basis", "")
    try:
        _service().create_default_book(actor=request.user, basis=basis)
    except (Forbidden, NotFound):
        raise Http404 from None
    except InvalidInput:
        messages.error(request, gettext("Escolha o regime do livro: caixa ou competência."))
    return redirect("skeleton_ledger:home", organization_id=organization_id)


@require_POST
@tenant_page()
@transaction.atomic
def lock(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    try:
        locked_until = _date(request.POST.get("locked_until"))
    except BadParameter as error:
        return _bad_request(request, organization_id, str(error))
    try:
        _service().set_lock(actor=request.user, locked_until=locked_until, expected_revision=_revision(request))
    except (Forbidden, NotFound):
        raise Http404 from None
    except Conflict as error:
        messages.error(request, _conflict_text(error))
    except InvalidInput:
        messages.error(request, gettext("Data inválida."))
    else:
        messages.success(request, gettext("Trava de período atualizada."))
    return redirect("skeleton_ledger:home", organization_id=organization_id)


@require_POST
@tenant_page()
@transaction.atomic
def member_access(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    from .services import grant_member_access

    try:
        added = grant_member_access(actor=request.user)
    except Forbidden:
        raise Http404 from None
    messages.success(
        request,
        gettext("Acesso de leitura concedido aos membros.") if added else gettext("Os membros já tinham acesso."),
    )
    return redirect("skeleton_ledger:home", organization_id=organization_id)


# --------------------------------------------------------------------------- chart of accounts


def _all_accounts(request: HttpRequest) -> list[Any]:
    found: list[Any] = []
    cursor = None
    while True:
        page = _service().accounts(actor=request.user, cursor=cursor, page_size=100)
        found.extend(page.items)
        cursor = page.next_cursor
        if cursor is None or len(found) >= 2000:
            return found


class AccountForm(forms.Form):
    code = forms.CharField(label=gettext_lazy("Código"), max_length=32)
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=150)
    kind = forms.ChoiceField(label=gettext_lazy("Tipo"), choices=KINDS)
    parent = forms.ChoiceField(label=gettext_lazy("Conta-grupo"), required=False)
    is_group = forms.BooleanField(label=gettext_lazy("É conta-grupo"), required=False)

    def __init__(self, *args: Any, accounts: list[Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        groups = [(str(item.sysid), f"{item.code} {item.name}") for item in accounts if item.is_group]
        _set_choices(self, "parent", [("", "—"), *groups])


class CostCenterForm(forms.Form):
    code = forms.CharField(label=gettext_lazy("Código"), max_length=32)
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=150)


@tenant_page()
def accounts(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    book = _default_book(request)
    if book is None:
        return redirect("skeleton_ledger:home", organization_id=organization_id)
    found = _all_accounts(request)
    account_form = AccountForm(request.POST or None, accounts=found, prefix="account")
    center_form = CostCenterForm(request.POST or None, prefix="center")
    status = 200
    target = request.POST.get("form") if request.method == "POST" else None
    if request.method == "POST":
        try:
            with transaction.atomic():
                if target == "account" and account_form.is_valid():
                    data = account_form.cleaned_data
                    _service().create_account(
                        actor=request.user,
                        code=data["code"],
                        name=data["name"],
                        kind=data["kind"],
                        parent_id=data["parent"] or None,
                        is_group=data["is_group"],
                    )
                    messages.success(request, gettext("Conta criada."))
                    return redirect("skeleton_ledger:accounts", organization_id=organization_id)
                if target == "center" and center_form.is_valid():
                    _service().create_cost_center(
                        actor=request.user, code=center_form.cleaned_data["code"], name=center_form.cleaned_data["name"]
                    )
                    messages.success(request, gettext("Centro de custo criado."))
                    return redirect("skeleton_ledger:accounts", organization_id=organization_id)
        except (Forbidden, NotFound):
            raise Http404 from None
        except InvalidInput as error:
            _apply_errors(account_form if target == "account" else center_form, error)
        except Conflict as error:
            (account_form if target == "account" else center_form).add_error(None, _conflict_text(error))
        status = 400
    return render(
        request,
        "skeleton_ledger/accounts.html",
        {
            "organization_id": organization_id,
            "book": book,
            "accounts": found,
            "keys": _service().account_keys(actor=request.user),
            "centers": _service().cost_centers(actor=request.user),
            "account_form": account_form if target == "account" else AccountForm(accounts=found, prefix="account"),
            "center_form": center_form if target == "center" else CostCenterForm(prefix="center"),
        },
        status=status,
    )


@require_POST
@tenant_page()
@transaction.atomic
def apply_chart(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    try:
        created = _service().apply_chart(actor=request.user)
    except (Forbidden, NotFound):
        raise Http404 from None
    except InvalidInput:
        messages.error(request, gettext("Modelo de plano de contas desconhecido."))
    else:
        messages.success(request, gettext("Plano de contas aplicado: %(count)s contas criadas.") % {"count": created})
    return redirect("skeleton_ledger:accounts", organization_id=organization_id)


@require_POST
@tenant_page()
@transaction.atomic
def account_active(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    active = request.POST.get("is_active") == "true"
    try:
        _service().update_account(
            actor=request.user, account_id=sysid, expected_revision=_revision(request), is_active=active
        )
    except (Forbidden, NotFound):
        raise Http404 from None
    except Conflict as error:
        messages.error(request, _conflict_text(error))
    except InvalidInput as error:
        messages.error(request, " ".join(text for texts in error.errors.values() for text in texts))
    else:
        messages.success(request, gettext("Conta ativada.") if active else gettext("Conta desativada."))
    return redirect("skeleton_ledger:accounts", organization_id=organization_id)


@require_POST
@tenant_page()
@transaction.atomic
def bind_key(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    try:
        account_id = UUIDValue(request.POST.get("account", ""))
    except ValueError:
        messages.error(request, gettext("Escolha a conta."))
        return redirect("skeleton_ledger:accounts", organization_id=organization_id)
    try:
        _service().bind_key(actor=request.user, key=request.POST.get("key", ""), account_id=account_id)
    except (Forbidden, NotFound):
        raise Http404 from None
    except InvalidInput as error:
        messages.error(request, " ".join(text for texts in error.errors.values() for text in texts))
    else:
        messages.success(request, gettext("Chave vinculada."))
    return redirect("skeleton_ledger:accounts", organization_id=organization_id)


# --------------------------------------------------------------------------- entries


@tenant_page()
def entries(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    book = _default_book(request)
    if book is None:
        return redirect("skeleton_ledger:home", organization_id=organization_id)
    try:
        start, end = _window(request)
        page = _service().entries(
            actor=request.user, start=start, end=end, cursor=request.GET.get("cursor") or None, page_size=50
        )
    except BadParameter as error:
        return _bad_request(request, organization_id, str(error))
    except InvalidInput:
        return _bad_request(request, organization_id, gettext("Página inválida."))
    except (Forbidden, NotFound):
        raise Http404 from None
    return render(
        request,
        "skeleton_ledger/entries.html",
        {"organization_id": organization_id, "book": book, "page": page, "start": start, "end": end},
    )


@tenant_page()
def entry_detail(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    book = _default_book(request)
    try:
        entry = _service().entry(actor=request.user, entry_id=sysid)
    except (Forbidden, NotFound, InvalidInput):
        raise Http404 from None
    names = {item.sysid: item for item in _all_accounts(request)}
    return render(
        request,
        "skeleton_ledger/entry.html",
        {
            "organization_id": organization_id,
            "book": book,
            "entry": entry,
            "lines": [(line, names.get(line.account_id)) for line in entry.lines],
            "today": timezone.localdate(),
        },
    )


@require_POST
@tenant_page()
@transaction.atomic
def entry_reverse(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    try:
        day = _day(request.POST.get("date"), timezone.localdate())
    except BadParameter as error:
        return _bad_request(request, organization_id, str(error))
    try:
        reversal = _service().reverse(
            actor=request.user,
            entry_id=sysid,
            date=day,
            reason=request.POST.get("reason", "")[:300],
            expected_revision=_revision(request),
        )
    except (Forbidden, NotFound):
        raise Http404 from None
    except Conflict as error:
        messages.error(request, _conflict_text(error))
    except InvalidInput as error:
        messages.error(request, " ".join(text for texts in error.errors.values() for text in texts))
    else:
        messages.success(request, gettext("Lançamento estornado."))
        return redirect("skeleton_ledger:entry", organization_id=organization_id, sysid=reversal.sysid)
    return redirect("skeleton_ledger:entry", organization_id=organization_id, sysid=sysid)


class ManualEntryForm(forms.Form):
    date = forms.DateField(label=gettext_lazy("Data"), widget=_date_input())
    description = forms.CharField(label=gettext_lazy("Descrição"), max_length=300)

    def __init__(self, *args: Any, accounts: list[Any], currency: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        choices = [("", "—")] + [
            (str(item.sysid), f"{item.code} {item.name}") for item in accounts if not item.is_group and item.is_active
        ]
        for number in range(1, MANUAL_LINES + 1):
            self.fields[f"account_{number}"] = forms.ChoiceField(
                label=gettext("Conta da partida %(number)s") % {"number": number}, choices=choices, required=False
            )
            self.fields[f"debit_{number}"] = AmountField(
                label=gettext("Débito da partida %(number)s") % {"number": number}, currency=currency, required=False
            )
            self.fields[f"credit_{number}"] = AmountField(
                label=gettext("Crédito da partida %(number)s") % {"number": number}, currency=currency, required=False
            )

    def line_rows(self) -> list[tuple[Any, Any, Any]]:
        return [
            (self[f"account_{number}"], self[f"debit_{number}"], self[f"credit_{number}"])
            for number in range(1, MANUAL_LINES + 1)
        ]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        lines = []
        for number in range(1, MANUAL_LINES + 1):
            account, debit, credit = (data.get(f"{name}_{number}") for name in ("account", "debit", "credit"))
            if not account and not debit and not credit:
                continue
            if not account:
                self.add_error(f"account_{number}", gettext("Escolha a conta da partida."))
                continue
            if bool(debit) == bool(credit):
                self.add_error(f"debit_{number}", gettext("Informe débito ou crédito, não os dois."))
                continue
            lines.append(PostingLine(UUIDValue(account), debit=debit or 0, credit=credit or 0))
        if len(lines) < 2 and not self.errors:
            raise forms.ValidationError(gettext("Um lançamento tem ao menos duas partidas."))
        data["lines"] = tuple(lines)
        return data


@tenant_page()
def entry_new(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    book = _default_book(request)
    if book is None:
        return redirect("skeleton_ledger:home", organization_id=organization_id)
    found = _all_accounts(request)
    form = ManualEntryForm(
        request.POST or None, accounts=found, currency=book.currency, initial={"date": timezone.localdate()}
    )
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    entry = _service().post_manual(
                        actor=request.user,
                        lines=form.cleaned_data["lines"],
                        date=form.cleaned_data["date"],
                        description=form.cleaned_data["description"],
                        idempotency_key=request.POST.get("idempotency_key", "")[:200],
                    )
            except (Forbidden, NotFound):
                raise Http404 from None
            except InvalidInput as error:
                _apply_errors(form, error)
            except Conflict as error:
                form.add_error(None, _conflict_text(error))
            else:
                messages.success(request, gettext("Lançamento registrado."))
                return redirect("skeleton_ledger:entry", organization_id=organization_id, sysid=entry.sysid)
        status = 400
    import uuid

    return render(
        request,
        "skeleton_ledger/entry_form.html",
        {
            "organization_id": organization_id,
            "book": book,
            "form": form,
            "idempotency_key": request.POST.get("idempotency_key") or uuid.uuid4().hex,
        },
        status=status,
    )


# --------------------------------------------------------------------------- titles


@tenant_page()
def titles(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    book = _default_book(request)
    if book is None:
        return redirect("skeleton_ledger:home", organization_id=organization_id)
    try:
        direction = _choice(request.GET.get("direction"), DIRECTIONS)
        status = _choice(request.GET.get("status"), STATUSES)
        due_until = _date(request.GET.get("due_until"))
        page = _service().titles(
            actor=request.user,
            direction=direction,
            status=status,
            due_until=due_until,
            cursor=request.GET.get("cursor") or None,
            page_size=50,
        )
    except BadParameter as error:
        return _bad_request(request, organization_id, str(error))
    except InvalidInput:
        return _bad_request(request, organization_id, gettext("Filtro ou página inválida."))
    except (Forbidden, NotFound):
        raise Http404 from None
    return render(
        request,
        "skeleton_ledger/titles.html",
        {
            "organization_id": organization_id,
            "book": book,
            "page": page,
            "directions": DIRECTIONS,
            "statuses": STATUSES,
            "filters": {"direction": direction or "", "status": status or "", "due_until": due_until},
        },
    )


def _category_choices(request: HttpRequest) -> list[tuple[str, str]]:
    keys = _service().account_keys(actor=request.user)
    return [(key.code, f"{key.label} ({key.code})") for key in keys if key.kind in ("revenue", "expense")]


class TitleForm(forms.Form):
    direction = forms.ChoiceField(label=gettext_lazy("Tipo"), choices=DIRECTIONS)
    description = forms.CharField(label=gettext_lazy("Descrição"), max_length=300)
    counterparty_name = forms.CharField(label=gettext_lazy("Contraparte"), max_length=200, required=False)
    category_key = forms.ChoiceField(label=gettext_lazy("Categoria"))
    issue_date = forms.DateField(label=gettext_lazy("Emissão"), widget=_date_input())
    due_date = forms.DateField(label=gettext_lazy("Vencimento (da primeira parcela)"), widget=_date_input())
    installments = forms.IntegerField(label=gettext_lazy("Parcelas"), min_value=1, max_value=120, initial=1)

    def __init__(self, *args: Any, categories: list[tuple[str, str]], currency: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _set_choices(self, "category_key", categories)
        self.fields["amount"] = AmountField(
            label=gettext("Valor total (%(currency)s)") % {"currency": currency}, currency=currency
        )


@tenant_page()
def title_new(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    import uuid

    book = _default_book(request)
    if book is None:
        return redirect("skeleton_ledger:home", organization_id=organization_id)
    today = timezone.localdate()
    form = TitleForm(
        request.POST or None,
        categories=_category_choices(request),
        currency=book.currency,
        initial={"issue_date": today, "due_date": today, "installments": 1},
    )
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            common = {
                "actor": request.user,
                "direction": data["direction"],
                "amount": data["amount"],
                "description": data["description"],
                "category_key": data["category_key"],
                "issue_date": data["issue_date"],
                "counterparty_name": data["counterparty_name"],
                "idempotency_key": request.POST.get("idempotency_key", "")[:150] or uuid.uuid4().hex,
            }
            try:
                with transaction.atomic():
                    if data["installments"] > 1:
                        created = _service().create_installments(
                            count=data["installments"], first_due_date=data["due_date"], **common
                        )[0]
                    else:
                        created = _service().create_title(due_date=data["due_date"], **common)
            except (Forbidden, NotFound):
                raise Http404 from None
            except InvalidInput as error:
                _apply_errors(form, error)
            except Conflict as error:
                form.add_error(None, _conflict_text(error))
            else:
                messages.success(request, gettext("Título registrado."))
                return redirect("skeleton_ledger:title", organization_id=organization_id, sysid=created.sysid)
        status = 400
    return render(
        request,
        "skeleton_ledger/title_form.html",
        {
            "organization_id": organization_id,
            "book": book,
            "form": form,
            "idempotency_key": request.POST.get("idempotency_key") or uuid.uuid4().hex,
        },
        status=status,
    )


class SettleForm(forms.Form):
    date = forms.DateField(label=gettext_lazy("Data"), widget=_date_input())
    payment_account = forms.ChoiceField(label=gettext_lazy("Conta de pagamento"))
    method = forms.ChoiceField(label=gettext_lazy("Forma"), choices=METHODS)

    def __init__(self, *args: Any, accounts: list[Any], currency: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _set_choices(
            self,
            "payment_account",
            [
                (str(item.sysid), f"{item.code} {item.name}")
                for item in accounts
                if item.kind == "asset" and not item.is_group and item.is_active
            ],
        )
        self.fields["amount"] = AmountField(
            label=gettext("Valor baixado (%(currency)s)") % {"currency": currency}, currency=currency
        )
        for name, label in (
            ("discount", gettext("Desconto")),
            ("interest", gettext("Juros")),
            ("fee", gettext("Tarifa")),
        ):
            self.fields[name] = AmountField(label=label, currency=currency, required=False)


@tenant_page()
def title_detail(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    import uuid

    book = _default_book(request)
    try:
        title = _service().title(actor=request.user, title_id=sysid)
        settlements = _service().settlements(actor=request.user, title_id=sysid)
    except (Forbidden, NotFound, InvalidInput):
        raise Http404 from None
    currency = book.currency if book is not None else "BRL"
    found = _all_accounts(request) if book is not None and book.writable else []
    form = SettleForm(
        request.POST or None,
        accounts=found,
        currency=currency,
        initial={"date": timezone.localdate(), "method": "pix", "amount": money.format_amount(title.balance, currency)},
    )
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                with transaction.atomic():
                    _service().settle_title(
                        actor=request.user,
                        title_id=sysid,
                        amount=data["amount"],
                        date=data["date"],
                        payment_account_id=data["payment_account"],
                        method=data["method"],
                        expected_revision=_revision(request),
                        idempotency_key=request.POST.get("idempotency_key", "")[:150] or uuid.uuid4().hex,
                        discount=data["discount"] or 0,
                        interest=data["interest"] or 0,
                        fee=data["fee"] or 0,
                    )
            except (Forbidden, NotFound):
                raise Http404 from None
            except InvalidInput as error:
                _apply_errors(form, error)
            except Conflict as error:
                form.add_error(None, _conflict_text(error))
            else:
                messages.success(request, gettext("Baixa registrada."))
                return redirect("skeleton_ledger:title", organization_id=organization_id, sysid=sysid)
        status = 400
    return render(
        request,
        "skeleton_ledger/title.html",
        {
            "organization_id": organization_id,
            "book": book,
            "currency": currency,
            "title": title,
            "settlements": settlements,
            "form": form,
            "can_settle": bool(found) and title.status in ("open", "partial"),
            "idempotency_key": request.POST.get("idempotency_key") or uuid.uuid4().hex,
            "today": timezone.localdate(),
        },
        status=status,
    )


@require_POST
@tenant_page()
@transaction.atomic
def title_cancel(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    try:
        _service().cancel_title(
            actor=request.user,
            title_id=sysid,
            expected_revision=_revision(request),
            reason=request.POST.get("reason", "")[:300],
            date=timezone.localdate(),
        )
    except (Forbidden, NotFound):
        raise Http404 from None
    except Conflict as error:
        messages.error(request, _conflict_text(error))
    except InvalidInput as error:
        messages.error(request, " ".join(text for texts in error.errors.values() for text in texts))
    else:
        messages.success(request, gettext("Título cancelado."))
    return redirect("skeleton_ledger:title", organization_id=organization_id, sysid=sysid)


@require_POST
@tenant_page()
@transaction.atomic
def settlement_reverse(request: HttpRequest, organization_id: UUID, sysid: UUID, settlement_id: UUID) -> HttpResponse:
    try:
        day = _day(request.POST.get("date"), timezone.localdate())
    except BadParameter as error:
        return _bad_request(request, organization_id, str(error))
    try:
        _service().reverse_settlement(
            actor=request.user,
            settlement_id=settlement_id,
            date=day,
            expected_revision=_revision(request),
            reason=request.POST.get("reason", "")[:300],
        )
    except (Forbidden, NotFound):
        raise Http404 from None
    except Conflict as error:
        messages.error(request, _conflict_text(error))
    except InvalidInput as error:
        messages.error(request, " ".join(text for texts in error.errors.values() for text in texts))
    else:
        messages.success(request, gettext("Baixa estornada."))
    return redirect("skeleton_ledger:title", organization_id=organization_id, sysid=sysid)


# --------------------------------------------------------------------------- recurrences


class RecurrenceForm(forms.Form):
    direction = forms.ChoiceField(label=gettext_lazy("Tipo"), choices=DIRECTIONS)
    description = forms.CharField(label=gettext_lazy("Descrição"), max_length=300)
    counterparty_name = forms.CharField(label=gettext_lazy("Contraparte"), max_length=200, required=False)
    category_key = forms.ChoiceField(label=gettext_lazy("Categoria"))
    frequency = forms.ChoiceField(label=gettext_lazy("Frequência"), choices=FREQUENCIES)
    interval = forms.IntegerField(label=gettext_lazy("A cada"), min_value=1, max_value=52, initial=1)
    day_of_month = forms.IntegerField(
        label=gettext_lazy("Dia do mês (mensal)"), min_value=1, max_value=31, required=False
    )
    start_date = forms.DateField(label=gettext_lazy("Início"), widget=_date_input())
    end_date = forms.DateField(label=gettext_lazy("Fim"), widget=_date_input(), required=False)
    max_occurrences = forms.IntegerField(label=gettext_lazy("Número máximo de títulos"), min_value=1, required=False)
    days_ahead = forms.IntegerField(
        label=gettext_lazy("Gerar com antecedência de (dias)"), min_value=0, max_value=366, initial=30
    )

    def __init__(self, *args: Any, categories: list[tuple[str, str]], currency: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _set_choices(self, "category_key", categories)
        self.fields["amount"] = AmountField(
            label=gettext("Valor (%(currency)s)") % {"currency": currency}, currency=currency
        )


class RecurrenceChangeForm(forms.Form):
    description = forms.CharField(label=gettext_lazy("Descrição"), max_length=300)
    end_date = forms.DateField(label=gettext_lazy("Fim"), widget=_date_input(), required=False)
    days_ahead = forms.IntegerField(label=gettext_lazy("Gerar com antecedência de (dias)"), min_value=0, max_value=366)

    def __init__(self, *args: Any, currency: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["amount"] = AmountField(
            label=gettext("Valor (%(currency)s)") % {"currency": currency}, currency=currency
        )


@tenant_page()
def recurrences(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    import uuid

    book = _default_book(request)
    if book is None:
        return redirect("skeleton_ledger:home", organization_id=organization_id)
    form = RecurrenceForm(
        request.POST or None,
        categories=_category_choices(request),
        currency=book.currency,
        initial={"start_date": timezone.localdate(), "interval": 1, "days_ahead": 30},
    )
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = dict(form.cleaned_data)
            try:
                with transaction.atomic():
                    _service().create_recurrence(
                        actor=request.user,
                        idempotency_key=request.POST.get("idempotency_key", "")[:150] or uuid.uuid4().hex,
                        **data,
                    )
            except (Forbidden, NotFound):
                raise Http404 from None
            except InvalidInput as error:
                _apply_errors(form, error)
            except Conflict as error:
                form.add_error(None, _conflict_text(error))
            else:
                messages.success(request, gettext("Recorrência criada."))
                return redirect("skeleton_ledger:recurrences", organization_id=organization_id)
        status = 400
    try:
        rules = _service().recurrences(actor=request.user)
    except (Forbidden, NotFound):
        raise Http404 from None
    return render(
        request,
        "skeleton_ledger/recurrences.html",
        {
            "organization_id": organization_id,
            "book": book,
            "rules": rules,
            "form": form,
            "idempotency_key": request.POST.get("idempotency_key") or uuid.uuid4().hex,
        },
        status=status,
    )


def _rule(request: HttpRequest, sysid: UUID) -> Any:
    try:
        rules = _service().recurrences(actor=request.user)
    except (Forbidden, NotFound):
        raise Http404 from None
    found = next((rule for rule in rules if rule.sysid == sysid), None)
    if found is None:
        raise Http404
    return found


@tenant_page()
def recurrence_detail(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    book = _default_book(request)
    rule = _rule(request, sysid)
    currency = book.currency if book is not None else "BRL"
    initial = {
        "description": rule.description,
        "end_date": rule.end_date,
        "days_ahead": rule.days_ahead,
        "amount": money.format_amount(rule.amount, currency),
    }
    form = RecurrenceChangeForm(request.POST or None, currency=currency, initial=initial)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    _service().update_recurrence(
                        actor=request.user,
                        recurrence_id=sysid,
                        data=dict(form.cleaned_data),
                        expected_revision=_revision(request),
                    )
            except (Forbidden, NotFound):
                raise Http404 from None
            except InvalidInput as error:
                _apply_errors(form, error)
            except Conflict as error:
                form.add_error(None, _conflict_text(error))
            else:
                messages.success(request, gettext("Recorrência alterada; vale para as próximas ocorrências."))
                return redirect("skeleton_ledger:recurrence", organization_id=organization_id, sysid=sysid)
        status = 400
    return render(
        request,
        "skeleton_ledger/recurrence.html",
        {"organization_id": organization_id, "book": book, "currency": currency, "rule": rule, "form": form},
        status=status,
    )


@require_POST
@tenant_page()
@transaction.atomic
def recurrence_toggle(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    operation = _service().resume if request.POST.get("action") == "resume" else _service().pause
    try:
        operation(actor=request.user, recurrence_id=sysid, expected_revision=_revision(request))
    except (Forbidden, NotFound):
        raise Http404 from None
    except Conflict as error:
        messages.error(request, _conflict_text(error))
    else:
        messages.success(request, gettext("Recorrência atualizada."))
    return redirect("skeleton_ledger:recurrence", organization_id=organization_id, sysid=sysid)


# --------------------------------------------------------------------------- reports

REPORTS = (
    ("trial-balance", gettext_lazy("Balancete")),
    ("statement", gettext_lazy("Razão por conta")),
    ("income", gettext_lazy("Resultado")),
    ("cash-flow", gettext_lazy("Fluxo de caixa")),
    ("aging", gettext_lazy("Títulos por atraso")),
)


@tenant_page()
def report(request: HttpRequest, organization_id: UUID, name: str = "trial-balance") -> HttpResponse:
    book = _default_book(request)
    if book is None:
        return redirect("skeleton_ledger:home", organization_id=organization_id)
    if name not in dict(REPORTS):
        raise Http404
    context: dict[str, Any] = {
        "organization_id": organization_id,
        "book": book,
        "reports": [
            (code, label, reverse("skeleton_ledger:report", args=(organization_id, code))) for code, label in REPORTS
        ],
        "current": name,
    }
    service = _service()
    try:
        if name == "aging":
            as_of = _day(request.GET.get("as_of"), timezone.localdate())
            direction = _choice(request.GET.get("direction"), DIRECTIONS) or "receivable"
            context.update(as_of=as_of, direction=direction, directions=DIRECTIONS)
            context["aging"] = service.aging(actor=request.user, as_of=as_of, direction=direction)
        else:
            start, end = _window(request)
            context.update(start=start, end=end)
            if name == "trial-balance":
                context["trial"] = service.trial_balance(actor=request.user, start=start, end=end)
            elif name == "income":
                context["income"] = service.income_statement(actor=request.user, start=start, end=end)
            elif name == "cash-flow":
                context["flow"] = service.cash_flow(actor=request.user, start=start, end=end)
            else:
                found = _all_accounts(request)
                context["accounts"] = found
                account = request.GET.get("account") or ""
                context["account"] = account
                if account:
                    if account not in {str(item.sysid) for item in found}:
                        raise BadParameter(gettext("Conta desconhecida."))
                    context["statement"] = service.account_statement(
                        actor=request.user,
                        account_id=UUIDValue(account),
                        start=start,
                        end=end,
                        cursor=request.GET.get("cursor") or None,
                        page_size=100,
                    )
    except BadParameter as error:
        return _bad_request(request, organization_id, str(error))
    except InvalidInput:
        return _bad_request(request, organization_id, gettext("Parâmetros inválidos."))
    except (Forbidden, NotFound):
        raise Http404 from None
    return render(request, "skeleton_ledger/report.html", context)
