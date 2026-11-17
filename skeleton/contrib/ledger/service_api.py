"""Service API of the ledger (docs/service-api.md): the only module other apps may import (#542).

Besides ``LedgerService``, it re-exports the registration functions an app calls in ``ready()``
to declare its account keys and posting rules (#543, #545).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied

from skeleton.service_api import ServiceAPI, service_method
from skeleton.service_api.dto import DEFAULT_PAGE_SIZE, Page, paginate

from .models import Account, AccountBinding, Book, CostCenter, Entry, EntryLine, RecurringTitle, Settlement, Title
from .posting import LineDraft as PostingLine
from .registry import AccountKey, Key, Param, PostingRule, RuleLine, register_account_key, register_posting_rule

APP_LABEL = "skeleton_ledger"
__all__ = [
    "AccountInfo",
    "AccountKey",
    "AccountKeyInfo",
    "AgingBucket",
    "AgingInfo",
    "BalanceInfo",
    "BookInfo",
    "CashFlowInfo",
    "CashFlowMonth",
    "CostCenterInfo",
    "EntryInfo",
    "EntryLineInfo",
    "IncomeStatementInfo",
    "LedgerService",
    "Key",
    "Param",
    "PostingLine",
    "PostingRule",
    "RecurrenceInfo",
    "ReportRow",
    "RuleLine",
    "SettlementInfo",
    "StatementInfo",
    "StatementLine",
    "TitleInfo",
    "TrialBalanceInfo",
    "register_posting_rule",
    "register_account_key",
]


@dataclass(frozen=True, slots=True)
class BookInfo:
    sysid: UUID
    name: str
    currency: str
    basis: str
    is_default: bool
    locked_until: date | None
    revision: int
    writable: bool = False  # Whether the actor manages the book (update on it).


def book_info(book: Book, *, writable: bool = False) -> BookInfo:
    return BookInfo(
        sysid=book.pk,
        name=book.name,
        currency=book.currency,
        basis=book.basis,
        is_default=book.is_default,
        locked_until=book.locked_until,
        revision=book.revision,
        writable=writable,
    )


@dataclass(frozen=True, slots=True)
class AccountInfo:
    sysid: UUID
    book_id: UUID
    code: str
    name: str
    kind: str
    parent_id: UUID | None
    is_group: bool
    is_active: bool
    revision: int


def account_info(account: Account) -> AccountInfo:
    return AccountInfo(
        account.pk,
        account.book_id,
        account.code,
        account.name,
        account.kind,
        account.parent_id,
        account.is_group,
        account.is_active,
        account.revision,
    )


@dataclass(frozen=True, slots=True)
class AccountKeyInfo:
    """A registered account key and the account the book binds it to (``None``: not bound)."""

    code: str
    label: str
    kind: str
    account_id: UUID | None


@dataclass(frozen=True, slots=True)
class CostCenterInfo:
    sysid: UUID
    book_id: UUID
    code: str
    name: str
    is_active: bool
    revision: int


def cost_center_info(center: CostCenter) -> CostCenterInfo:
    return CostCenterInfo(center.pk, center.book_id, center.code, center.name, center.is_active, center.revision)


@dataclass(frozen=True, slots=True)
class EntryLineInfo:
    sysid: UUID
    account_id: UUID
    debit: int
    credit: int
    memo: str
    cost_center_id: UUID | None
    party_ref: str
    party_kind: str


@dataclass(frozen=True, slots=True)
class EntryInfo:
    sysid: UUID
    book_id: UUID
    year: int
    number: int
    date: date
    description: str
    kind: str
    rule: str
    source_app: str
    source_table: str
    source_id: UUID | None
    reverses_id: UUID | None
    reversed_by_id: UUID | None
    total: int
    sealed: bool
    digest: str
    lines: tuple[EntryLineInfo, ...]
    revision: int = 0


def entry_info(entry: Entry) -> EntryInfo:
    lines = tuple(
        EntryLineInfo(
            line.pk,
            line.account_id,
            line.debit,
            line.credit,
            line.memo,
            line.cost_center_id,
            line.party_ref,
            line.party_kind,
        )
        for line in EntryLine.objects.filter(entry=entry).order_by("sysid")
    )
    reversal = Entry.objects.filter(reverses=entry).values_list("pk", flat=True).first()
    return EntryInfo(
        sysid=entry.pk,
        book_id=entry.book_id,
        year=entry.year,
        number=entry.number,
        date=entry.date,
        description=entry.description,
        kind=entry.kind,
        rule=entry.rule,
        source_app=entry.source_app,
        source_table=entry.source_table,
        source_id=entry.source_id,
        reverses_id=entry.reverses_id,
        reversed_by_id=reversal,
        total=entry.total,
        sealed=entry.sealed_at is not None,
        digest=entry.seal_digest,
        lines=lines,
        revision=entry.revision,
    )


@dataclass(frozen=True, slots=True)
class TitleInfo:
    sysid: UUID
    book_id: UUID
    direction: str
    description: str
    counterparty_name: str
    party_ref: str
    party_kind: str
    category_account_id: UUID
    cost_center_id: UUID | None
    amount: int
    amount_settled: int
    balance: int
    issue_date: date
    due_date: date
    status: str
    source_table: str
    source_id: UUID | None
    installment_number: int | None
    installment_count: int | None
    revision: int


def title_info(title: Title) -> TitleInfo:
    return TitleInfo(
        sysid=title.pk,
        book_id=title.book_id,
        direction=title.direction,
        description=title.description,
        counterparty_name=title.counterparty_name,
        party_ref=title.party_ref,
        party_kind=title.party_kind,
        category_account_id=title.category_account_id,
        cost_center_id=title.cost_center_id,
        amount=title.amount,
        amount_settled=title.amount_settled,
        balance=title.balance,
        issue_date=title.issue_date,
        due_date=title.due_date,
        status=title.status,
        source_table=title.source_table,
        source_id=title.source_id,
        installment_number=title.installment_number,
        installment_count=title.installment_count,
        revision=title.revision,
    )


@dataclass(frozen=True, slots=True)
class SettlementInfo:
    sysid: UUID
    title_id: UUID
    date: date
    amount: int
    discount: int
    interest: int
    fee: int
    payment_account_id: UUID
    method: str
    entry_id: UUID
    reversal_entry_id: UUID | None
    revision: int


def settlement_info(settlement: Settlement) -> SettlementInfo:
    return SettlementInfo(
        settlement.pk,
        settlement.title_id,
        settlement.date,
        settlement.amount,
        settlement.discount,
        settlement.interest,
        settlement.fee,
        settlement.payment_account_id,
        settlement.method,
        settlement.entry_id,
        settlement.reversal_entry_id,
        settlement.revision,
    )


@dataclass(frozen=True, slots=True)
class RecurrenceInfo:
    sysid: UUID
    book_id: UUID
    direction: str
    description: str
    counterparty_name: str
    category_account_id: UUID
    cost_center_id: UUID | None
    amount: int
    frequency: str
    interval: int
    day_of_month: int | None
    start_date: date
    end_date: date | None
    max_occurrences: int | None
    next_due: date | None
    generated_count: int
    days_ahead: int
    is_active: bool
    ended: bool
    revision: int


def recurrence_info(rule: RecurringTitle) -> RecurrenceInfo:
    from .recurrence import ended

    return RecurrenceInfo(
        sysid=rule.pk,
        book_id=rule.book_id,
        direction=rule.direction,
        description=rule.description,
        counterparty_name=rule.counterparty_name,
        category_account_id=rule.category_account_id,
        cost_center_id=rule.cost_center_id,
        amount=rule.amount,
        frequency=rule.frequency,
        interval=rule.interval,
        day_of_month=rule.day_of_month,
        start_date=rule.start_date,
        end_date=rule.end_date,
        max_occurrences=rule.max_occurrences,
        next_due=None if ended(rule, rule.next_due) else rule.next_due,
        generated_count=rule.generated_count,
        days_ahead=rule.days_ahead,
        is_active=rule.is_active,
        ended=ended(rule, rule.next_due),
        revision=rule.revision,
    )


@dataclass(frozen=True, slots=True)
class BalanceInfo:
    """Balance of an account on ``as_of``, in the natural sign of its kind (groups sum descendants)."""

    account_id: UUID
    code: str
    name: str
    kind: str
    is_group: bool
    as_of: date
    debit: int
    credit: int
    balance: int


@dataclass(frozen=True, slots=True)
class ReportRow:
    """One account of a trial balance or an income statement: ``closing = opening ± (debit - credit)``."""

    account_id: UUID
    code: str
    name: str
    kind: str
    parent_id: UUID | None
    is_group: bool
    opening: int
    debit: int
    credit: int
    closing: int


@dataclass(frozen=True, slots=True)
class TrialBalanceInfo:
    start: date
    end: date
    rows: tuple[ReportRow, ...]
    total_debit: int
    total_credit: int


@dataclass(frozen=True, slots=True)
class IncomeStatementInfo:
    start: date
    end: date
    revenue: int
    expense: int
    result: int
    rows: tuple[ReportRow, ...]


@dataclass(frozen=True, slots=True)
class StatementLine:
    line_id: UUID
    entry_id: UUID
    date: date
    year: int
    number: int
    description: str
    memo: str
    debit: int
    credit: int
    balance: int


@dataclass(frozen=True, slots=True)
class StatementInfo:
    account_id: UUID
    start: date
    end: date
    opening: int
    page: Page[StatementLine]


@dataclass(frozen=True, slots=True)
class CashFlowMonth:
    month: date
    realized_in: int
    realized_out: int
    forecast_in: int
    forecast_out: int


@dataclass(frozen=True, slots=True)
class CashFlowInfo:
    start: date
    end: date
    opening: int
    realized_in: int
    realized_out: int
    closing: int
    forecast_in: int
    forecast_out: int
    months: tuple[CashFlowMonth, ...]


@dataclass(frozen=True, slots=True)
class AgingBucket:
    name: str
    amount: int
    count: int


@dataclass(frozen=True, slots=True)
class AgingInfo:
    as_of: date
    direction: str
    total: int
    buckets: tuple[AgingBucket, ...]


def report_row(node: Any) -> ReportRow:
    account = node.account
    return ReportRow(
        account_id=account.pk,
        code=account.code,
        name=account.name,
        kind=account.kind,
        parent_id=account.parent_id,
        is_group=account.is_group,
        opening=node.opening,
        debit=node.debit,
        credit=node.credit,
        closing=node.closing,
    )


class LedgerService(ServiceAPI):
    """Books, chart of accounts, entries, titles and reports of the current organization."""

    app_label = APP_LABEL

    @service_method(read_only=True, since="0.1.0")
    def books(self, *, actor: Any) -> tuple[BookInfo, ...]:
        """Books the actor reads in the current organization, the default one first.

        Autorização: TablePermission read em skeleton_ledger.book e visibilidade dos registros.
        Erros: Forbidden sem a permissão de tabela; Unavailable com a app desativada.
        Efeitos: nenhum.
        """
        from skeleton.service_api import Forbidden

        from .policies import readable_books, writable_books

        try:
            rows = list(readable_books(actor))
            writable = set(writable_books(actor).values_list("pk", flat=True))
        except PermissionDenied:
            raise Forbidden() from None
        return tuple(book_info(book, writable=book.pk in writable) for book in rows)

    @service_method(read_only=False, since="0.1.0")
    def create_default_book(self, *, actor: Any, basis: str = "cash", currency: str = "BRL") -> BookInfo:
        """The organization's default book, created on the first call (one per organization).

        Autorização: TablePermission create em skeleton_ledger.book para criar; quem só lê recebe o
        livro existente se o ler.
        Erros: Forbidden sem a permissão de criar quando ainda não há livro padrão; NotFound se o livro
        padrão existe mas o ator não o lê; InvalidInput para regime ou moeda inválidos.
        Efeitos: cria o livro padrão (moeda e regime imutáveis) na transação do chamador.
        """
        from skeleton.service_api import Forbidden, InvalidInput, NotFound

        from .policies import can_manage, readable_books
        from .services import ensure_default_book

        if basis not in Book.Basis.values:
            raise InvalidInput({"basis": ["Use cash ou accrual."]})
        try:
            book = ensure_default_book(actor, basis=basis, currency=currency)
        except PermissionDenied:
            raise Forbidden() from None
        if not readable_books(actor).filter(pk=book.pk).exists():
            raise NotFound()
        return book_info(book, writable=can_manage(actor, book))

    # ----------------------------------------------------------- chart of accounts (#543)

    @service_method(read_only=True, since="0.1.0")
    def accounts(
        self, *, actor: Any, book_id: UUID | None = None, cursor: str | None = None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> Page[AccountInfo]:
        """The chart of accounts of a book, by code.

        Autorização: leitura do livro.
        Erros: NotFound para livro inexistente, ilegível ou de outra organização; InvalidInput para
        cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        book = _readable_book(actor, book_id)
        return paginate(
            Account.objects.filter(book=book).order_by("code", "sysid"),
            convert=account_info,
            page_size=page_size,
            cursor=cursor,
        )

    @service_method(read_only=False, since="0.1.0")
    def apply_chart(self, *, actor: Any, book_id: UUID | None = None, template: str = "services") -> int:
        """Create the accounts and the base key bindings of a chart template in a book; idempotent.

        Autorização: gestão do livro (update).
        Erros: NotFound; Forbidden se só lê; InvalidInput para modelo desconhecido.
        Efeitos: cria só o que falta (contas e vínculos) e devolve quantas linhas criou.
        """
        from skeleton.service_api import InvalidInput

        from .chart import TEMPLATES, apply_chart

        if template not in TEMPLATES:
            raise InvalidInput({"template": ["Modelo de plano de contas desconhecido."]})
        return apply_chart(_managed_book(actor, book_id), template)

    @service_method(read_only=False, since="0.1.0")
    def create_account(
        self,
        *,
        actor: Any,
        code: str,
        name: str,
        kind: str,
        book_id: UUID | None = None,
        parent_id: UUID | None = None,
        is_group: bool = False,
    ) -> AccountInfo:
        """Create an account; the parent is a group account of the same book and kind.

        Autorização: gestão do livro.
        Erros: NotFound; Forbidden; InvalidInput para código repetido, tipo inválido ou pai de outro
        livro, de outro tipo ou que não é grupo.
        Efeitos: grava a conta (auditada) na transação do chamador.
        """
        from skeleton.service_api import as_uuid

        book = _managed_book(actor, book_id)
        account = Account(
            organization_id=book.organization_id,
            book=book,
            code=code,
            name=name,
            kind=kind,
            parent_id=as_uuid(parent_id, field="parent_id") if parent_id is not None else None,
            is_group=bool(is_group),
        )
        account.save()
        return account_info(account)

    @service_method(read_only=False, since="0.1.0")
    def update_account(
        self,
        *,
        actor: Any,
        account_id: UUID,
        expected_revision: int,
        name: str | None = None,
        is_active: bool | None = None,
    ) -> AccountInfo:
        """Rename or (de)activate an account, with CAS; code, kind, parent and group never change.

        Autorização: gestão do livro da conta.
        Erros: NotFound; Forbidden; Conflict("stale_revision"); InvalidInput.
        Efeitos: grava a alteração.
        """
        from skeleton.service_api import Conflict

        account = _account(actor, account_id, manage=True)
        if type(expected_revision) is not int or account.revision != expected_revision:
            raise Conflict("stale_revision")
        changed: set[str] = set()
        if name is not None:
            account.name = name
            changed.add("name")
        if is_active is not None:
            account.is_active = bool(is_active)
            changed.add("is_active")
        account.save(update_fields=changed)
        return account_info(account)

    @service_method(read_only=False, since="0.1.0")
    def delete_account(self, *, actor: Any, account_id: UUID, expected_revision: int) -> None:
        """Soft delete an account without children, bindings or lines (live references), with CAS.

        Autorização: gestão do livro da conta.
        Erros: NotFound; Forbidden; Conflict("stale_revision"); Conflict("live_references").
        Efeitos: exclusão lógica da conta.
        """
        from skeleton.service_api import Conflict

        account = _account(actor, account_id, manage=True)
        if type(expected_revision) is not int or account.revision != expected_revision:
            raise Conflict("stale_revision")
        account.delete()

    @service_method(read_only=True, since="0.1.0")
    def account_keys(self, *, actor: Any, book_id: UUID | None = None) -> tuple[AccountKeyInfo, ...]:
        """Registered account keys of active apps with the account the book binds each to.

        Autorização: leitura do livro.
        Erros: NotFound.
        Efeitos: nenhum.
        """
        from .registry import all_keys

        book = _readable_book(actor, book_id)
        bound = dict(AccountBinding.objects.filter(book=book).values_list("key", "account_id"))
        return tuple(
            AccountKeyInfo(key.code, str(key.label), key.kind, bound.get(key.code))
            for key in all_keys()
            if _app_active(key.app_label)
        )

    @service_method(read_only=False, since="0.1.0")
    def bind_key(self, *, actor: Any, key: str, account_id: UUID, book_id: UUID | None = None) -> AccountKeyInfo:
        """Bind an account key to an analytic, active account of the book of the key's kind.

        Autorização: gestão do livro.
        Erros: NotFound; Forbidden; InvalidInput para chave desconhecida ou de app desativada, conta
        de outro livro, conta-grupo, inativa ou de outro tipo.
        Efeitos: cria o vínculo ou troca a conta dele.
        """
        from skeleton.service_api import InvalidInput

        from .registry import get_key

        book = _managed_book(actor, book_id)
        try:
            declared = get_key(key)
        except KeyError:
            raise InvalidInput({"key": ["Chave de conta desconhecida."]}) from None
        if not _app_active(declared.app_label):
            raise InvalidInput({"key": ["A app da chave não está ativa."]})
        account = Account.objects.filter(pk=_uuid(account_id, "account_id"), book=book).first()
        if account is None or account.is_group or not account.is_active or account.kind != declared.kind:
            raise InvalidInput({"account_id": ["Use uma conta analítica, ativa, deste livro e do tipo da chave."]})
        binding = AccountBinding.objects.filter(book=book, key=key).first()
        if binding is None:
            AccountBinding(organization_id=book.organization_id, book=book, key=key, account=account).save()
        elif binding.account_id != account.pk:
            binding.account = account
            binding.save(update_fields={"account"})
        return AccountKeyInfo(declared.code, str(declared.label), declared.kind, account.pk)

    @service_method(read_only=True, since="0.1.0")
    def cost_centers(self, *, actor: Any, book_id: UUID | None = None) -> tuple[CostCenterInfo, ...]:
        """Cost centers of a book (at most 500).

        Autorização: leitura do livro.
        Erros: NotFound.
        Efeitos: nenhum.
        """
        book = _readable_book(actor, book_id)
        return tuple(cost_center_info(row) for row in CostCenter.objects.filter(book=book)[:500])

    @service_method(read_only=False, since="0.1.0")
    def create_cost_center(self, *, actor: Any, code: str, name: str, book_id: UUID | None = None) -> CostCenterInfo:
        """Create a cost center in a book.

        Autorização: gestão do livro.
        Erros: NotFound; Forbidden; InvalidInput para código repetido ou vazio.
        Efeitos: grava o centro de custo.
        """
        book = _managed_book(actor, book_id)
        center = CostCenter(organization_id=book.organization_id, book=book, code=code, name=name)
        center.save()
        return cost_center_info(center)

    # ----------------------------------------------------------- entries (#544)

    @service_method(read_only=False, since="0.1.0")
    def post_manual(
        self,
        *,
        actor: Any,
        lines: tuple[PostingLine, ...],
        date: date,
        description: str,
        idempotency_key: str,
        book_id: UUID | None = None,
    ) -> EntryInfo:
        """Post a manual entry with explicit lines: created, numbered and sealed in one transaction.

        Autorização: gestão do livro (update).
        Erros: NotFound; Forbidden; Conflict("period_locked") com a data na trava; InvalidInput para
        partidas que não fecham, uma só partida, conta-grupo, inativa ou de outro livro, data ou
        chave inválidas.
        Efeitos: grava o lançamento, as partidas e o selo na transação do chamador; a mesma
        ``idempotency_key`` devolve o lançamento já criado.
        """
        book = _managed_book(actor, book_id)
        return _post(
            lambda: _create(
                book=book,
                lines=lines,
                date=date,
                description=description,
                kind=Entry.Kind.MANUAL,
                actor=actor,
                idempotency_key=_key(idempotency_key),
            )
        )

    @service_method(read_only=False, since="0.1.0", allow_system=True)
    def reverse(
        self,
        *,
        actor: Any,
        entry_id: UUID,
        date: date,
        reason: str = "",
        idempotency_key: str = "",
        expected_revision: int | None = None,
    ) -> EntryInfo:
        """Reverse an entry once: the inverse entry, linked by ``reverses``; a reversal is not reversible.

        Autorização: gestão do livro do lançamento; um lançamento com origem (#545) também pela ação
        da regra no registro de origem, sem ler o livro; ``actor=None`` só em ``system_context``.
        Erros: NotFound; Forbidden; Conflict("already_reversed"); Conflict("period_locked");
        Conflict("stale_revision") com ``expected_revision`` divergente; InvalidInput para estornar
        um estorno.
        Efeitos: grava o estorno selado na transação do chamador.
        """
        from skeleton.service_api import Conflict

        from . import posting

        entry = _entry_to_change(actor, entry_id)
        if expected_revision is not None and entry.revision != expected_revision:
            raise Conflict("stale_revision")
        return _post(
            lambda: posting.reverse_entry(
                entry=entry,
                date=date,
                reason=reason,
                actor_id=getattr(actor, "pk", None),
                idempotency_key=_key(idempotency_key, required=False),
            )
        )

    @service_method(read_only=False, since="0.1.0", allow_system=True)
    def post(
        self,
        *,
        actor: Any,
        rule: str,
        amounts: dict[str, int],
        date: date,
        description: str,
        source_id: UUID,
        idempotency_key: str,
        accounts: dict[str, UUID] | None = None,
        book_id: UUID | None = None,
        cost_center_id: UUID | None = None,
        party_ref: str = "",
        party_kind: str = "",
    ) -> EntryInfo:
        """Post an entry by a rule declared in code, authorized by the source record (#545).

        Whoever may act on the source record (an appointment, a sale) posts without reading the book.
        The description cites the number of the source document, never the name of a person.
        Autorização: a ação da regra (``source_action``) no registro ``source_id`` da tabela de origem
        dela; ``actor=None`` só em ``system_context`` (a origem precisa existir na organização).
        Erros: NotFound para origem inexistente, ilegível ou de outra organização, ou sem livro
        padrão; Forbidden sem a ação na origem; Unavailable com a app da origem desativada;
        Conflict("account_not_bound") com a chave sem vínculo no livro; Conflict("period_locked");
        InvalidInput para regra desconhecida, valores inválidos, ``Param`` ausente ou de outro livro e
        resultado desbalanceado.
        Efeitos: cria, parte e sela o lançamento na transação do chamador; a mesma chave da mesma
        app de origem devolve o lançamento já criado.
        """
        from . import posting

        definition = _rule(rule)
        _authorize_source(actor, definition.source_table_key, source_id, definition.source_action)
        book = _posting_book(book_id)
        key = _key(idempotency_key)
        found = posting.existing_entry(definition.app_label, key)
        if found is not None:
            return entry_info(found)
        lines = _rule_lines(book, definition, amounts, accounts or {}, cost_center_id, party_ref, party_kind)
        return _post(
            lambda: _create(
                book=book,
                lines=lines,
                date=date,
                description=description,
                kind=Entry.Kind.RULE,
                actor=actor,
                rule=definition.code,
                source_app=definition.app_label,
                source_table=definition.source_table_key,
                source_id=_uuid(source_id, "source_id"),
                idempotency_key=key,
            )
        )

    @service_method(read_only=True, since="0.1.0")
    def entries_for_source(self, *, actor: Any, table_key: str, record_id: UUID) -> tuple[EntryInfo, ...]:
        """The sealed entries of one source record (at most 200), for the app to show them next to it.

        Autorização: leitura do registro de origem (não exige leitura do livro).
        Erros: NotFound para origem inexistente, ilegível ou de outra organização; InvalidInput.
        Efeitos: nenhum.
        """
        _authorize_source(actor, table_key, record_id, "read")
        rows = Entry.objects.filter(
            source_table=table_key, source_id=_uuid(record_id, "record_id"), sealed_at__isnull=False
        ).order_by("date", "number", "sysid")[:200]
        return tuple(entry_info(row) for row in rows)

    # ----------------------------------------------------------- titles and settlements (#546)

    @service_method(read_only=False, since="0.1.0", allow_system=True)
    def create_title(
        self,
        *,
        actor: Any,
        direction: str,
        amount: int,
        due_date: date,
        description: str,
        category_key: str,
        idempotency_key: str,
        issue_date: date | None = None,
        counterparty_name: str = "",
        party_ref: str = "",
        party_kind: str = "",
        source_table: str | None = None,
        source_id: UUID | None = None,
        cost_center_id: UUID | None = None,
        book_id: UUID | None = None,
    ) -> TitleInfo:
        """Create a receivable or a payable; in an accrual book it posts the recognition entry.

        Autorização: com origem, ``update`` no registro de origem (sem leitura do livro); sem origem,
        gestão do livro; ``actor=None`` só em ``system_context``.
        Erros: NotFound; Forbidden; Unavailable com a app da origem desativada;
        Conflict("account_not_bound") com a chave da categoria ou de contrapartida sem vínculo;
        InvalidInput para direção, valor, datas, descrição ou chave inválidos.
        Efeitos: grava o título (e o lançamento de reconhecimento) na transação do chamador; a mesma
        ``idempotency_key`` da mesma origem devolve o título já criado.
        """
        values = _title_values(
            actor,
            direction=direction,
            amount=amount,
            due_date=due_date,
            description=description,
            issue_date=issue_date,
            counterparty_name=counterparty_name,
            party_ref=party_ref,
            party_kind=party_kind,
            source_table=source_table,
            source_id=source_id,
            cost_center_id=cost_center_id,
        )
        book = _title_book(actor, source_table, source_id, book_id)
        key = _key(idempotency_key)
        existing = Title.objects.filter(source_app=values["source_app"], idempotency_key=key).first()
        if existing is not None:
            return title_info(existing)
        values["category_account"] = _category(book, category_key)
        return _title_call(lambda: _create_titles(actor, book, [{**values, "idempotency_key": key}])[0])

    @service_method(read_only=False, since="0.1.0", allow_system=True)
    def create_installments(
        self,
        *,
        actor: Any,
        direction: str,
        amount: int,
        count: int,
        first_due_date: date,
        description: str,
        category_key: str,
        idempotency_key: str,
        issue_date: date | None = None,
        counterparty_name: str = "",
        party_ref: str = "",
        party_kind: str = "",
        source_table: str | None = None,
        source_id: UUID | None = None,
        cost_center_id: UUID | None = None,
        book_id: UUID | None = None,
    ) -> tuple[TitleInfo, ...]:
        """Split ``amount`` in ``count`` monthly titles; the rounding rest goes to the last one.

        Due dates keep the day of ``first_due_date``, capped at the last day of shorter months.
        Autorização: a de ``create_title``.
        Erros: os de ``create_title``; InvalidInput para ``count`` fora de 2 a 120.
        Efeitos: grava as parcelas na transação do chamador; a mesma ``idempotency_key`` devolve as já
        criadas.
        """
        from skeleton.service_api import InvalidInput

        from . import titles

        if type(count) is not int or not 2 <= count <= 120:
            raise InvalidInput({"count": ["Use de 2 a 120 parcelas."]})
        values = _title_values(
            actor,
            direction=direction,
            amount=amount,
            due_date=first_due_date,
            description=description,
            issue_date=issue_date,
            counterparty_name=counterparty_name,
            party_ref=party_ref,
            party_kind=party_kind,
            source_table=source_table,
            source_id=source_id,
            cost_center_id=cost_center_id,
        )
        if amount < count:
            raise InvalidInput({"amount": ["Valor menor que o número de parcelas."]})
        book = _title_book(actor, source_table, source_id, book_id)
        key = _key(idempotency_key)
        existing = Title.objects.filter(source_app=values["source_app"], idempotency_key__startswith=f"{key}#")
        if existing.exists():
            return tuple(title_info(row) for row in existing.order_by("installment_number"))
        values["category_account"] = _category(book, category_key)
        rows = []
        for number, part in enumerate(titles.split(amount, count), start=1):
            rows.append(
                {
                    **values,
                    "amount": part,
                    "due_date": titles.add_months(first_due_date, number - 1),
                    "installment_number": number,
                    "installment_count": count,
                    "idempotency_key": f"{key}#{number}",
                    "description": f"{values['description']} ({number}/{count})"[:300],
                }
            )
        return tuple(_title_call(lambda: _create_titles(actor, book, rows)))

    @service_method(read_only=False, since="0.1.0", allow_system=True)
    def settle_title(
        self,
        *,
        actor: Any,
        title_id: UUID,
        amount: int,
        date: date,
        payment_account_id: UUID,
        method: str,
        expected_revision: int,
        idempotency_key: str,
        discount: int = 0,
        interest: int = 0,
        fee: int = 0,
    ) -> SettlementInfo:
        """Receive or pay all or part of a title's balance, posting the settlement entry.

        Autorização: a do título (origem ou gestão do livro).
        Erros: NotFound; Forbidden; Conflict("stale_revision") com a revisão do título divergente;
        Conflict("title_settled") com o título já baixado; Conflict("period_locked");
        Conflict("account_not_bound"); InvalidInput para valor acima do saldo, valores negativos,
        conta de pagamento inválida ou forma desconhecida.
        Efeitos: grava a baixa, o lançamento e o saldo do título na transação do chamador; a mesma
        ``idempotency_key`` no mesmo título devolve a baixa já feita.
        """
        from skeleton.service_api import Conflict

        from . import titles

        title = _title_to_change(actor, title_id)
        key = _key(idempotency_key)
        done = Settlement.objects.filter(title=title, idempotency_key=key).first()
        if done is not None:
            return settlement_info(done)
        if title.revision != expected_revision:
            raise Conflict("stale_revision")
        if title.status == Title.Status.SETTLED:
            raise Conflict("title_settled")
        account = Account.objects.filter(pk=_uuid(payment_account_id, "payment_account_id"), book=title.book).first()
        if account is None:
            from skeleton.service_api import InvalidInput

            raise InvalidInput({"payment_account_id": ["Use uma conta do livro."]})
        return _title_call(
            lambda: settlement_info(
                titles.settle(
                    title=title,
                    actor_id=getattr(actor, "pk", None),
                    amount=amount,
                    date=date,
                    payment_account=account,
                    method=method,
                    discount=discount,
                    interest=interest,
                    fee=fee,
                    idempotency_key=key,
                )
            )
        )

    @service_method(read_only=False, since="0.1.0", allow_system=True)
    def reverse_settlement(
        self, *, actor: Any, settlement_id: UUID, date: date, expected_revision: int, reason: str = ""
    ) -> TitleInfo:
        """Reverse a settlement: its entry is reversed and the balance goes back; the row stays.

        Autorização: a do título.
        Erros: NotFound; Forbidden; Conflict("stale_revision") com a revisão do título divergente;
        Conflict("already_reversed"); Conflict("period_locked").
        Efeitos: grava o estorno e o saldo na transação do chamador.
        """
        from skeleton.service_api import Conflict

        from . import titles

        settlement = Settlement.objects.filter(pk=_uuid(settlement_id, "settlement_id")).first()
        if settlement is None:
            from skeleton.service_api import NotFound

            raise NotFound()
        title = _title_to_change(actor, settlement.title_id)
        if title.revision != expected_revision:
            raise Conflict("stale_revision")
        if settlement.reversal_entry_id is not None:
            raise Conflict("already_reversed")
        return _title_call(
            lambda: title_info(
                titles.reverse_settlement(
                    settlement=settlement, title=title, actor_id=getattr(actor, "pk", None), date=date, reason=reason
                )
            )
        )

    @service_method(read_only=False, since="0.1.0", allow_system=True)
    def cancel_title(
        self, *, actor: Any, title_id: UUID, expected_revision: int, reason: str = "", date: date | None = None
    ) -> TitleInfo:
        """Cancel a title without live settlements; in an accrual book its recognition is reversed.

        Autorização: a do título.
        Erros: NotFound; Forbidden; Conflict("stale_revision"); Conflict("title_has_settlements");
        Conflict("period_locked").
        Efeitos: grava o cancelamento (e o estorno do reconhecimento) na transação do chamador.
        """
        from django.utils import timezone

        from skeleton.service_api import Conflict

        from . import titles

        title = _title_to_change(actor, title_id)
        if title.revision != expected_revision:
            raise Conflict("stale_revision")
        when = date or timezone.localdate()
        return _title_call(
            lambda: title_info(
                titles.cancel(title=title, actor_id=getattr(actor, "pk", None), date=when, reason=reason)
            )
        )

    @service_method(read_only=True, since="0.1.0")
    def titles(
        self,
        *,
        actor: Any,
        book_id: UUID | None = None,
        direction: str | None = None,
        status: str | None = None,
        due_until: date | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[TitleInfo]:
        """Titles of a book, by due date, filtered by direction, status and due date.

        Autorização: leitura do livro.
        Erros: NotFound; InvalidInput para filtro, cursor ou página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.service_api import InvalidInput

        book = _readable_book(actor, book_id)
        rows = Title.objects.filter(book=book)
        if direction is not None:
            if direction not in Title.Direction.values:
                raise InvalidInput({"direction": ["Use receivable ou payable."]})
            rows = rows.filter(direction=direction)
        if status is not None:
            if status not in Title.Status.values:
                raise InvalidInput({"status": ["Situação desconhecida."]})
            rows = rows.filter(status=status)
        if due_until is not None:
            rows = rows.filter(due_date__lte=due_until)
        return paginate(rows.order_by("due_date", "sysid"), convert=title_info, page_size=page_size, cursor=cursor)

    @service_method(read_only=True, since="0.1.0")
    def titles_for_source(self, *, actor: Any, table_key: str, record_id: UUID) -> tuple[TitleInfo, ...]:
        """Titles of one source record (at most 200): whoever reads the source knows its counterparty.

        Autorização: leitura do registro de origem.
        Erros: NotFound; InvalidInput.
        Efeitos: nenhum.
        """
        _authorize_source(actor, table_key, record_id, "read")
        rows = Title.objects.filter(source_table=table_key, source_id=_uuid(record_id, "record_id")).order_by(
            "due_date", "installment_number", "sysid"
        )[:200]
        return tuple(title_info(row) for row in rows)

    @service_method(read_only=True, since="0.1.0")
    def title(self, *, actor: Any, title_id: UUID) -> TitleInfo:
        """One title, for its page.

        Autorização: leitura do livro, ou do registro de origem quando há origem.
        Erros: NotFound para inexistente, ilegível ou de outra organização.
        Efeitos: nenhum.
        """
        return title_info(_title_to_read(actor, title_id))

    @service_method(read_only=True, since="0.1.0")
    def settlements(self, *, actor: Any, title_id: UUID) -> tuple[SettlementInfo, ...]:
        """Settlements of a title, oldest first.

        Autorização: leitura do livro do título, ou do registro de origem dele.
        Erros: NotFound.
        Efeitos: nenhum.
        """
        title = _title_to_read(actor, title_id)
        return tuple(settlement_info(row) for row in Settlement.objects.filter(title=title).order_by("date", "sysid"))

    @service_method(read_only=True, since="0.1.0")
    def entry(self, *, actor: Any, entry_id: UUID) -> EntryInfo:
        """One sealed entry with its lines.

        Autorização: leitura do livro.
        Erros: NotFound para lançamento inexistente, ilegível ou de outra organização.
        Efeitos: nenhum.
        """
        return entry_info(_entry(actor, entry_id))

    @service_method(read_only=True, since="0.1.0")
    def entries(
        self,
        *,
        actor: Any,
        start: date,
        end: date,
        book_id: UUID | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[EntryInfo]:
        """Sealed entries of a book in ``[start, end]`` (at most five years), by date and number.

        Autorização: leitura do livro.
        Erros: NotFound; InvalidInput para janela inválida, cursor ou página.
        Efeitos: nenhum.
        """
        book = _readable_book(actor, book_id)
        start, end = _date_window(start, end)
        rows = Entry.objects.filter(book=book, sealed_at__isnull=False, date__gte=start, date__lte=end).order_by(
            "date", "number", "sysid"
        )
        return paginate(rows, convert=entry_info, page_size=page_size, cursor=cursor)

    @service_method(read_only=False, since="0.1.0")
    def create_recurrence(
        self,
        *,
        actor: Any,
        direction: str,
        amount: int,
        description: str,
        category_key: str,
        frequency: str,
        start_date: date,
        idempotency_key: str,
        interval: int = 1,
        day_of_month: int | None = None,
        end_date: date | None = None,
        max_occurrences: int | None = None,
        days_ahead: int = 30,
        counterparty_name: str = "",
        party_ref: str = "",
        party_kind: str = "",
        cost_center_id: UUID | None = None,
        book_id: UUID | None = None,
    ) -> RecurrenceInfo:
        """Create a rule that generates a title per occurrence, ``days_ahead`` days before it falls due.

        ``frequency`` is ``weekly``, ``monthly`` or ``yearly``, every ``interval``; a monthly rule may fix
        ``day_of_month`` (days past the end of a shorter month fall on its last day). The rule ends at
        ``end_date`` or after ``max_occurrences`` titles. The job ``ledger.recurrences`` generates the
        titles daily.
        Autorização: gestão do livro.
        Erros: NotFound; Forbidden; Conflict("account_not_bound"); InvalidInput.
        Efeitos: grava a recorrência na transação do chamador; a mesma ``idempotency_key`` devolve a
        recorrência já criada.
        """
        from .recurrence import first_due

        values = _title_values(
            actor,
            direction=direction,
            amount=amount,
            due_date=start_date,
            description=description,
            issue_date=start_date,
            counterparty_name=counterparty_name,
            party_ref=party_ref,
            party_kind=party_kind,
            source_table=None,
            source_id=None,
            cost_center_id=cost_center_id,
        )
        book = _managed_book(actor, book_id)
        key = _key(idempotency_key)
        existing = RecurringTitle.objects.filter(idempotency_key=key).first()
        if existing is not None:
            return recurrence_info(existing)
        schedule = _schedule(
            frequency=frequency,
            interval=interval,
            day_of_month=day_of_month,
            start_date=start_date,
            end_date=end_date,
            max_occurrences=max_occurrences,
            days_ahead=days_ahead,
        )
        rule = RecurringTitle(
            organization_id=book.organization_id,
            book=book,
            direction=values["direction"],
            description=values["description"],
            counterparty_name=values["counterparty_name"],
            party_ref=values["party_ref"],
            party_kind=values["party_kind"],
            category_account=_category(book, category_key),
            cost_center_id=_book_cost_center(book, values["cost_center_id"]),
            amount=values["amount"],
            idempotency_key=key,
            **schedule,
        )
        rule.next_due = first_due(rule)
        rule.save()
        return recurrence_info(rule)

    @service_method(read_only=False, since="0.1.0")
    def update_recurrence(
        self, *, actor: Any, recurrence_id: UUID, data: dict[str, Any], expected_revision: int
    ) -> RecurrenceInfo:
        """Change the next occurrences of a rule, with CAS; the titles already generated stay as they are.

        ``data`` accepts ``amount``, ``description``, ``counterparty_name``, ``party_ref``,
        ``party_kind``, ``cost_center_id``, ``end_date``, ``max_occurrences`` and ``days_ahead``.
        Extending the end date or the occurrence limit of a rule that ended resumes it.
        Autorização: gestão do livro.
        Erros: NotFound; Forbidden; Conflict("stale_revision"); InvalidInput.
        Efeitos: grava a alteração (auditada) na transação do chamador.
        """
        from skeleton.service_api import InvalidInput

        allowed = {
            "amount",
            "description",
            "counterparty_name",
            "party_ref",
            "party_kind",
            "cost_center_id",
            "end_date",
            "max_occurrences",
            "days_ahead",
        }
        if not isinstance(data, dict) or not data or set(data) - allowed:
            raise InvalidInput({"data": [f"Campos aceitos: {', '.join(sorted(allowed))}."]})
        rule = _recurrence_to_change(actor, recurrence_id, expected_revision)
        merged = {
            "direction": rule.direction,
            "amount": data.get("amount", rule.amount),
            "due_date": rule.start_date,
            "issue_date": rule.start_date,
            "description": data.get("description", rule.description),
            "counterparty_name": data.get("counterparty_name", rule.counterparty_name),
            "party_ref": data.get("party_ref", rule.party_ref),
            "party_kind": data.get("party_kind", rule.party_kind),
            "source_table": None,
            "source_id": None,
            "cost_center_id": data.get("cost_center_id", rule.cost_center_id),
        }
        values = _title_values(actor, **merged)
        schedule = _schedule(
            frequency=rule.frequency,
            interval=rule.interval,
            day_of_month=rule.day_of_month,
            start_date=rule.start_date,
            end_date=data.get("end_date", rule.end_date),
            max_occurrences=data.get("max_occurrences", rule.max_occurrences),
            days_ahead=data.get("days_ahead", rule.days_ahead),
        )
        rule.amount = values["amount"]
        rule.description = values["description"]
        rule.counterparty_name = values["counterparty_name"]
        rule.party_ref = values["party_ref"]
        rule.party_kind = values["party_kind"]
        rule.cost_center_id = _book_cost_center(rule.book, values["cost_center_id"])
        rule.end_date = schedule["end_date"]
        rule.max_occurrences = schedule["max_occurrences"]
        rule.days_ahead = schedule["days_ahead"]
        rule.save()
        return recurrence_info(rule)

    @service_method(read_only=False, since="0.1.0")
    def pause(self, *, actor: Any, recurrence_id: UUID, expected_revision: int) -> RecurrenceInfo:
        """Stop generating the titles of a rule, with CAS.

        Autorização: gestão do livro.
        Erros: NotFound; Forbidden; Conflict("stale_revision").
        Efeitos: grava a pausa; os títulos já gerados ficam.
        """
        rule = _recurrence_to_change(actor, recurrence_id, expected_revision)
        if rule.is_active:
            rule.is_active = False
            rule.save(update_fields={"is_active"})
        return recurrence_info(rule)

    @service_method(read_only=False, since="0.1.0")
    def resume(self, *, actor: Any, recurrence_id: UUID, expected_revision: int) -> RecurrenceInfo:
        """Generate the titles of a paused rule again, with CAS.

        The occurrences that fell due while it was paused are skipped: the job generates from today
        on, up to ``days_ahead`` days ahead.
        Autorização: gestão do livro.
        Erros: NotFound; Forbidden; Conflict("stale_revision").
        Efeitos: grava a retomada.
        """
        from django.utils import timezone

        from .recurrence import skip_past

        rule = _recurrence_to_change(actor, recurrence_id, expected_revision)
        if not rule.is_active:
            rule.is_active = True
            rule.next_due = skip_past(rule, timezone.localdate())
            rule.save(update_fields={"is_active", "next_due"})
        return recurrence_info(rule)

    @service_method(read_only=True, since="0.1.0")
    def recurrences(self, *, actor: Any, book_id: UUID | None = None) -> tuple[RecurrenceInfo, ...]:
        """The recurring titles of a book.

        Autorização: leitura do livro.
        Erros: NotFound.
        Efeitos: nenhum.
        """
        book = _readable_book(actor, book_id)
        return tuple(recurrence_info(rule) for rule in RecurringTitle.objects.filter(book=book))

    # ----------------------------------------------------------- balances and reports (#548)

    @service_method(read_only=True, since="0.1.0")
    def balance(
        self,
        *,
        actor: Any,
        as_of: date,
        account_id: UUID | None = None,
        account_key: str | None = None,
        book_id: UUID | None = None,
    ) -> BalanceInfo:
        """Balance of one account (by id or by bound key) on ``as_of``; a group sums its descendants.

        Autorização: leitura do livro.
        Erros: NotFound (livro ou conta); Conflict("account_not_bound") com chave sem vínculo;
        InvalidInput sem exatamente um de ``account_id`` e ``account_key``, ou data inválida.
        Efeitos: nenhum.
        """
        from skeleton.service_api import Conflict, InvalidInput, NotFound

        from . import reports

        book = _readable_book(actor, book_id)
        as_of = _as_of(as_of)
        if (account_id is None) == (account_key is None):
            raise InvalidInput({"account_id": ["Informe account_id ou account_key."]})
        if account_key is not None:
            target = (
                AccountBinding.objects.filter(book=book, key=_key(account_key))
                .values_list("account_id", flat=True)
                .first()
            )
            if target is None:
                raise Conflict("account_not_bound", f"Vincule a chave {account_key} a uma conta do livro.")
        else:
            target = _uuid(account_id, "account_id")
        _, nodes = reports.totals(book, start=None, end=as_of)
        node = nodes.get(target)
        if node is None:
            raise NotFound()
        account = node.account
        return BalanceInfo(
            account.pk,
            account.code,
            account.name,
            account.kind,
            account.is_group,
            as_of,
            node.debit,
            node.credit,
            node.closing,
        )

    @service_method(read_only=True, since="0.1.0")
    def trial_balance(self, *, actor: Any, start: date, end: date, book_id: UUID | None = None) -> TrialBalanceInfo:
        """Trial balance of ``[start, end]`` (at most five years): every account, groups summing descendants.

        Totals sum the analytic accounts only, so debits equal credits in any window.
        Autorização: leitura do livro.
        Erros: NotFound; InvalidInput para janela inválida.
        Efeitos: nenhum.
        """
        from . import reports

        book = _readable_book(actor, book_id)
        start, end = _date_window(start, end)
        accounts, nodes = reports.totals(book, start=start, end=end)
        rows = tuple(report_row(nodes[account.pk]) for account in accounts)
        analytic = [row for row in rows if not row.is_group]
        return TrialBalanceInfo(
            start, end, rows, sum(row.debit for row in analytic), sum(row.credit for row in analytic)
        )

    @service_method(read_only=True, since="0.1.0")
    def income_statement(
        self, *, actor: Any, start: date, end: date, book_id: UUID | None = None
    ) -> IncomeStatementInfo:
        """Revenue minus expenses of ``[start, end]`` (at most five years), with the accounts that moved.

        Autorização: leitura do livro.
        Erros: NotFound; InvalidInput para janela inválida.
        Efeitos: nenhum.
        """
        from . import reports

        book = _readable_book(actor, book_id)
        start, end = _date_window(start, end)
        accounts, nodes = reports.totals(book, start=start, end=end)
        rows, revenue, expense = [], 0, 0
        for account in accounts:
            if account.kind not in ("revenue", "expense"):
                continue
            node = nodes[account.pk]
            movement = node.sign * (node.debit - node.credit)
            row = report_row(node)
            rows.append(ReportRow(**{**_row_dict(row), "opening": 0, "closing": movement}))
            if not account.is_group:
                if account.kind == "revenue":
                    revenue += movement
                else:
                    expense += movement
        return IncomeStatementInfo(start, end, revenue, expense, revenue - expense, tuple(rows))

    @service_method(read_only=True, since="0.1.0")
    def account_statement(
        self,
        *,
        actor: Any,
        account_id: UUID,
        start: date,
        end: date,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        book_id: UUID | None = None,
    ) -> StatementInfo:
        """Ledger of one account (with its descendants) in ``[start, end]``, by date, number and line.

        Each line carries the running balance, in the natural sign of the account, from the opening
        balance of ``start``.
        Autorização: leitura do livro.
        Erros: NotFound (livro ou conta); InvalidInput para janela, cursor ou página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.service_api import NotFound

        from . import reports

        book = _readable_book(actor, book_id)
        start, end = _date_window(start, end)
        target = _uuid(account_id, "account_id")
        accounts = reports.chart(book)
        account = next((item for item in accounts if item.pk == target), None)
        if account is None:
            raise NotFound()
        opening, rows = reports.statement_rows(book, accounts, account, start=start, end=end)

        def convert(line: Any) -> StatementLine:
            return StatementLine(
                line.pk,
                line.entry_id,
                line.date,
                line.entry.year,
                line.entry.number,
                line.entry.description,
                line.memo,
                line.debit,
                line.credit,
                opening + line.running,
            )

        page = paginate(rows, convert=convert, page_size=page_size, cursor=cursor)
        return StatementInfo(account.pk, start, end, opening, page)

    @service_method(read_only=True, since="0.1.0")
    def cash_flow(self, *, actor: Any, start: date, end: date, book_id: UUID | None = None) -> CashFlowInfo:
        """Realized cash flow (accounts bound to ``ledger.cash`` and ``ledger.bank``) and the forecast.

        The forecast is the open balance of the titles falling due in the window, by month.
        Autorização: leitura do livro.
        Erros: NotFound; InvalidInput para janela inválida.
        Efeitos: nenhum.
        """
        from . import reports

        book = _readable_book(actor, book_id)
        start, end = _date_window(start, end)
        flow = reports.cash_flow(book, start=start, end=end)
        months = tuple(CashFlowMonth(month, **values) for month, values in flow["months"].items())
        realized_in = sum(month.realized_in for month in months)
        realized_out = sum(month.realized_out for month in months)
        return CashFlowInfo(
            start,
            end,
            flow["opening"],
            realized_in,
            realized_out,
            flow["opening"] + realized_in - realized_out,
            sum(month.forecast_in for month in months),
            sum(month.forecast_out for month in months),
            months,
        )

    @service_method(read_only=True, since="0.1.0")
    def aging(
        self, *, actor: Any, as_of: date, direction: str = "receivable", book_id: UUID | None = None
    ) -> AgingInfo:
        """Open balance of the titles by overdue bucket on ``as_of``: current, 1-30, 31-60, 61-90, 90+ days.

        Uses the current open balance of each title issued on or before ``as_of``.
        Autorização: leitura do livro.
        Erros: NotFound; InvalidInput para data ou direção inválida.
        Efeitos: nenhum.
        """
        from skeleton.service_api import InvalidInput

        from . import reports

        book = _readable_book(actor, book_id)
        as_of = _as_of(as_of)
        if direction not in Title.Direction.values:
            raise InvalidInput({"direction": ["Use receivable ou payable."]})
        found = reports.aging(book, as_of=as_of, direction=direction)
        buckets = tuple(AgingBucket(name, *found[name]) for name in reports.AGING_BUCKETS)
        return AgingInfo(as_of, direction, sum(bucket.amount for bucket in buckets), buckets)

    @service_method(read_only=False, since="0.1.0")
    def set_lock(
        self, *, actor: Any, locked_until: date | None, expected_revision: int, book_id: UUID | None = None
    ) -> BookInfo:
        """Move the period lock of a book (entries on or before it are refused), with CAS.

        Autorização: gestão do livro.
        Erros: NotFound; Forbidden; Conflict("stale_revision"); InvalidInput para data inválida.
        Efeitos: grava a trava (auditada); recuar a trava também.
        """
        from . import posting

        book = _managed_book(actor, book_id)
        return book_info(
            posting.set_lock(book=book, locked_until=locked_until, expected_revision=expected_revision), writable=True
        )


def _readable_book(actor: Any, book_id: Any = None) -> Book:
    """The book ``book_id`` (the default one when ``None``) if the actor reads it; NotFound otherwise."""
    from skeleton.service_api import NotFound, as_uuid

    from .policies import readable_books

    try:
        rows = readable_books(actor)
    except PermissionDenied:
        raise NotFound() from None
    book = (
        rows.filter(is_default=True).first()
        if book_id is None
        else rows.filter(pk=as_uuid(book_id, field="book_id")).first()
    )
    if book is None:
        raise NotFound()
    return book


def _managed_book(actor: Any, book_id: Any = None) -> Book:
    """A readable book the actor manages; Forbidden when it only reads it."""
    from skeleton.service_api import Forbidden

    from .policies import can_manage

    book = _readable_book(actor, book_id)
    if not can_manage(actor, book):
        raise Forbidden()
    return book


def _uuid(value: Any, field: str) -> UUID:
    from skeleton.service_api import as_uuid

    return as_uuid(value, field=field)


def _account(actor: Any, account_id: Any, *, manage: bool = False) -> Account:
    """The account if the actor reads its book (NotFound) and, with ``manage``, manages it (Forbidden)."""
    from skeleton.service_api import Forbidden, NotFound

    from .policies import can_manage, readable_books

    try:
        account = (
            Account.objects.select_related("book")
            .filter(pk=_uuid(account_id, "account_id"), book__in=readable_books(actor))
            .first()
        )
    except PermissionDenied:
        account = None
    if account is None:
        raise NotFound()
    if manage and not can_manage(actor, account.book):
        raise Forbidden()
    return account


def _app_active(app_label: str) -> bool:
    """Whether the app that owns a key or a rule is active in the organization (#543)."""
    from skeleton.system.applications import application_state

    if app_label == APP_LABEL:
        return True
    try:
        state = application_state(app_label)
    except KeyError:
        return False
    return bool(state["effective"])


MAX_WINDOW_DAYS = 5 * 366


def _date_window(start: Any, end: Any) -> tuple[date, date]:
    from datetime import datetime

    from skeleton.service_api import InvalidInput

    if not all(isinstance(value, date) and not isinstance(value, datetime) for value in (start, end)):
        raise InvalidInput({"start": ["Use datas."]})
    if end < start or (end - start).days > MAX_WINDOW_DAYS:
        raise InvalidInput({"end": ["Use uma janela de até cinco anos, com o fim depois do início."]})
    return start, end


def _key(value: Any, *, required: bool = True) -> str:
    import re

    from skeleton.service_api import InvalidInput

    if not required and value in ("", None):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:._-]{1,200}", value):
        raise InvalidInput({"idempotency_key": ["Use de 1 a 200 letras, dígitos e : . _ -."]})
    return value


def _create(*, book: Book, lines: Any, date: Any, description: Any, kind: str, actor: Any, **values: Any) -> Entry:
    from . import posting

    return posting.create_entry(
        book=book,
        lines=lines,
        date=date,
        description=description,
        kind=kind,
        actor_id=getattr(actor, "pk", None),
        **values,
    )


def _post(function: Any) -> EntryInfo:
    """Run a posting and translate its refusals to the public errors."""
    from skeleton.service_api import Conflict

    from . import posting

    try:
        return entry_info(function())
    except posting.PeriodLocked:
        raise Conflict("period_locked") from None
    except posting.AlreadyReversed:
        raise Conflict("already_reversed") from None


def _entry(actor: Any, entry_id: Any, *, manage: bool = False) -> Entry:
    """A sealed entry of a book the actor reads (NotFound) and, with ``manage``, manages (Forbidden)."""
    from skeleton.service_api import Forbidden, NotFound

    from .policies import can_manage, readable_books

    try:
        entry = (
            Entry.objects.select_related("book")
            .filter(pk=_uuid(entry_id, "entry_id"), book__in=readable_books(actor), sealed_at__isnull=False)
            .first()
        )
    except PermissionDenied:
        entry = None
    if entry is None:
        raise NotFound()
    if manage and not can_manage(actor, entry.book):
        raise Forbidden()
    return entry


def _rule(code: Any) -> PostingRule:
    from skeleton.service_api import InvalidInput

    from .registry import get_rule

    try:
        return get_rule(code)
    except (KeyError, TypeError):
        raise InvalidInput({"rule": ["Regra de lançamento desconhecida."]}) from None


def _authorize_source(actor: Any, table_key: str, record_id: Any, action: str) -> None:
    """The actor may ``action`` the source record; the system context needs the record in the tenant.

    ``AccessService.require`` decides as ``require_record_action`` does, so a source of a derived
    table (``TableSpec.access_parent``) is authorized through its anchor (#565).
    """
    from django.apps import apps

    from skeleton.service_api import NotFound, Unavailable
    from skeleton.service_api.access import AccessService
    from skeleton.system.applications import require_application_enabled

    try:
        model = apps.get_model(table_key)
    except (LookupError, ValueError, TypeError, AttributeError):
        raise NotFound() from None
    try:
        require_application_enabled(model._meta.app_label)
    except PermissionDenied:
        raise Unavailable() from None
    identifier = _uuid(record_id, "record_id")
    if actor is None:
        if not model.objects.filter(pk=identifier).exists():
            raise NotFound()
        return
    AccessService().require(actor=actor, action=action, table_key=table_key, record_id=identifier)


def _posting_book(book_id: Any) -> Book:
    """The book an authorized posting goes to, read without the poster's book permission (#545)."""
    from skeleton.service_api import NotFound

    rows = Book.objects.all()
    book = (
        rows.filter(is_default=True).first() if book_id is None else rows.filter(pk=_uuid(book_id, "book_id")).first()
    )
    if book is None:
        raise NotFound()
    return book


def _rule_lines(
    book: Book,
    rule: PostingRule,
    amounts: Any,
    accounts: Any,
    cost_center_id: Any,
    party_ref: Any,
    party_kind: Any,
) -> tuple[PostingLine, ...]:
    """Resolve the accounts and amounts of a rule; a zero component drops its lines."""
    from skeleton.service_api import Conflict, InvalidInput

    if not isinstance(amounts, dict) or not all(type(value) is int and value >= 0 for value in amounts.values()):
        raise InvalidInput({"amounts": ["Valores são inteiros não negativos da menor unidade da moeda."]})
    if not isinstance(accounts, dict):
        raise InvalidInput({"accounts": ["Informe as contas por nome de parâmetro."]})
    if (
        not isinstance(party_ref, str)
        or len(party_ref) > 100
        or not isinstance(party_kind, str)
        or len(party_kind) > 50
    ):
        raise InvalidInput({"party_ref": ["Referência opaca de até 100 caracteres."]})
    bindings = dict(AccountBinding.objects.filter(book=book).values_list("key", "account_id"))
    center = _uuid(cost_center_id, "cost_center_id") if cost_center_id is not None else None
    lines = []
    for line in rule.lines:
        amount = amounts.get(line.amount, 0)
        if amount == 0:
            continue
        if isinstance(line.account, Key):
            account_id = bindings.get(line.account.code)
            if account_id is None:
                raise Conflict("account_not_bound", f"Vincule a chave {line.account.code} a uma conta do livro.")
        else:
            if line.account.name not in accounts:
                raise InvalidInput({"accounts": [f"Informe a conta {line.account.name}."]})
            account_id = _uuid(accounts[line.account.name], "accounts")
        lines.append(
            PostingLine(
                account_id=account_id,
                debit=amount if line.side == "debit" else 0,
                credit=amount if line.side == "credit" else 0,
                cost_center_id=center,
                party_ref=party_ref,
                party_kind=party_kind,
            )
        )
    return tuple(lines)


def _entry_to_change(actor: Any, entry_id: Any) -> Entry:
    """An entry to reverse: by the source record of its rule, or by the management of its book."""
    from skeleton.service_api import NotFound

    from .registry import get_rule

    entry = Entry.objects.select_related("book").filter(pk=_uuid(entry_id, "entry_id"), sealed_at__isnull=False).first()
    if entry is None:
        raise NotFound()
    if entry.source_table and entry.source_id is not None:
        try:
            action = get_rule(entry.rule).source_action
        except KeyError:
            action = "update"
        _authorize_source(actor, entry.source_table, entry.source_id, action)
        return entry
    if actor is None:
        raise NotFound()
    return _entry(actor, entry.pk, manage=True)


def _title_values(actor: Any, **raw: Any) -> dict[str, Any]:
    """Validated scalar fields of a new title (the book and the category are resolved apart)."""
    from datetime import datetime

    from django.utils import timezone

    from skeleton.service_api import InvalidInput

    if raw["direction"] not in Title.Direction.values:
        raise InvalidInput({"direction": ["Use receivable ou payable."]})
    if type(raw["amount"]) is not int or raw["amount"] <= 0:
        raise InvalidInput({"amount": ["Valor inteiro positivo da menor unidade da moeda."]})
    issue = raw["issue_date"] or timezone.localdate()
    for name, value in (("due_date", raw["due_date"]), ("issue_date", issue)):
        if not isinstance(value, date) or isinstance(value, datetime):
            raise InvalidInput({name: ["Use uma data."]})
    description = raw["description"]
    if not isinstance(description, str) or not description.strip() or len(description) > 300:
        raise InvalidInput({"description": ["Descreva o título (até 300 caracteres)."]})
    for name, limit in (("counterparty_name", 200), ("party_ref", 100), ("party_kind", 50)):
        if not isinstance(raw[name], str) or len(raw[name]) > limit:
            raise InvalidInput({name: [f"Até {limit} caracteres."]})
    source_table, source_id = raw["source_table"], raw["source_id"]
    if (source_table is None) != (source_id is None):
        raise InvalidInput({"source_table": ["Informe tabela e registro de origem juntos."]})
    return {
        "direction": raw["direction"],
        "amount": raw["amount"],
        "due_date": raw["due_date"],
        "issue_date": issue,
        "description": description.strip(),
        "counterparty_name": raw["counterparty_name"],
        "party_ref": raw["party_ref"],
        "party_kind": raw["party_kind"],
        "source_app": source_table.split(".", 1)[0] if source_table else "skeleton_ledger",
        "source_table": source_table or "",
        "source_id": _uuid(source_id, "source_id") if source_id is not None else None,
        "cost_center_id": _uuid(raw["cost_center_id"], "cost_center_id") if raw["cost_center_id"] is not None else None,
    }


def _title_book(actor: Any, source_table: Any, source_id: Any, book_id: Any) -> Book:
    """Book of a new title: authorized by the source record when there is one, else by the book."""
    if source_table:
        _authorize_source(actor, source_table, source_id, "update")
        return _posting_book(book_id)
    if actor is None:
        return _posting_book(book_id)
    return _managed_book(actor, book_id)


def _category(book: Book, key: Any) -> Account:
    from skeleton.service_api import Conflict, InvalidInput

    from .registry import get_key

    try:
        get_key(key)
    except (KeyError, TypeError):
        raise InvalidInput({"category_key": ["Chave de conta desconhecida."]}) from None
    account = Account.objects.filter(bindings__book=book, bindings__key=key, bindings__deleted_at__isnull=True).first()
    if account is None:
        raise Conflict("account_not_bound", f"Vincule a chave {key} a uma conta do livro.")
    return account


def _create_titles(actor: Any, book: Book, rows: list[dict[str, Any]]) -> list[TitleInfo]:
    from django.db import transaction

    from skeleton.tenancy.context import current_context

    from . import titles

    created = []
    with transaction.atomic(using=current_context().using):
        for values in rows:
            created.append(
                title_info(titles.create_title(book=book, actor_id=getattr(actor, "pk", None), values=values))
            )
    return created


def _title_call(function: Any) -> Any:
    """Run a title operation and translate its refusals to the public errors."""
    from skeleton.service_api import Conflict

    from . import posting, titles

    try:
        return function()
    except titles.AccountNotBound as error:
        raise Conflict("account_not_bound", f"Vincule a chave {error.key} a uma conta do livro.") from None
    except titles.TitleHasSettlements:
        raise Conflict("title_has_settlements") from None
    except posting.PeriodLocked:
        raise Conflict("period_locked") from None
    except posting.AlreadyReversed:
        raise Conflict("already_reversed") from None


def _title_to_change(actor: Any, title_id: Any) -> Title:
    """A title locked for a change: by its source record, or by the management of its book."""
    from skeleton.service_api import NotFound

    title = Title.objects.select_related("book").filter(pk=_uuid(title_id, "title_id")).first()
    if title is None:
        raise NotFound()
    if title.source_table and title.source_id is not None:
        _authorize_source(actor, title.source_table, title.source_id, "update")
    elif actor is not None:
        _managed_book(actor, title.book_id)
    return Title.objects.select_related("book").select_for_update(of=("self",)).get(pk=title.pk)


def _title_to_read(actor: Any, title_id: Any) -> Title:
    from skeleton.service_api import NotFound

    title = Title.objects.select_related("book").filter(pk=_uuid(title_id, "title_id")).first()
    if title is None:
        raise NotFound()
    if title.source_table and title.source_id is not None:
        try:
            _authorize_source(actor, title.source_table, title.source_id, "read")
            return title
        except NotFound:
            pass
    _readable_book(actor, title.book_id)
    return title


_FREQUENCIES = ("weekly", "monthly", "yearly")


def _schedule(
    *,
    frequency: Any,
    interval: Any,
    day_of_month: Any,
    start_date: Any,
    end_date: Any,
    max_occurrences: Any,
    days_ahead: Any,
) -> dict[str, Any]:
    """Validated schedule fields of a recurring title."""
    from datetime import datetime

    from skeleton.service_api import InvalidInput

    def integer(value: Any, low: int, high: int) -> bool:
        return type(value) is int and low <= value <= high

    errors: dict[str, list[str]] = {}
    if frequency not in _FREQUENCIES:
        errors["frequency"] = ["Use weekly, monthly ou yearly."]
    if not integer(interval, 1, 52):
        errors["interval"] = ["Use de 1 a 52."]
    if day_of_month is not None and (frequency != "monthly" or not integer(day_of_month, 1, 31)):
        errors["day_of_month"] = ["Só em recorrência mensal, de 1 a 31."]
    for name, value in (("start_date", start_date), ("end_date", end_date)):
        if value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
            errors[name] = ["Use uma data."]
    if start_date is None:
        errors["start_date"] = ["Use uma data."]
    if "start_date" not in errors and "end_date" not in errors and end_date is not None and end_date < start_date:
        errors["end_date"] = ["O fim vem depois do início."]
    if max_occurrences is not None and not integer(max_occurrences, 1, 100000):
        errors["max_occurrences"] = ["Use um inteiro positivo."]
    if not integer(days_ahead, 0, 366):
        errors["days_ahead"] = ["Use de 0 a 366 dias."]
    if errors:
        raise InvalidInput(errors)
    return {
        "frequency": frequency,
        "interval": interval,
        "day_of_month": day_of_month,
        "start_date": start_date,
        "end_date": end_date,
        "max_occurrences": max_occurrences,
        "days_ahead": days_ahead,
    }


def _book_cost_center(book: Book, cost_center_id: Any) -> Any:
    from skeleton.service_api import InvalidInput

    if cost_center_id is None:
        return None
    if not CostCenter.objects.filter(pk=cost_center_id, book=book, is_active=True).exists():
        raise InvalidInput({"cost_center_id": ["Use um centro de custo ativo do livro."]})
    return cost_center_id


def _recurrence_to_change(actor: Any, recurrence_id: Any, expected_revision: Any) -> RecurringTitle:
    from skeleton.service_api import Conflict, NotFound

    rule = RecurringTitle.objects.filter(pk=_uuid(recurrence_id, "recurrence_id")).first()
    if rule is None:
        raise NotFound()
    _managed_book(actor, rule.book_id)
    rule = RecurringTitle.objects.select_related("book").select_for_update(of=("self",)).get(pk=rule.pk)
    if type(expected_revision) is not int or rule.revision != expected_revision:
        raise Conflict("stale_revision", "A recorrência mudou.")
    return rule


def _as_of(value: Any) -> date:
    from datetime import datetime

    from skeleton.service_api import InvalidInput

    if not isinstance(value, date) or isinstance(value, datetime):
        raise InvalidInput({"as_of": ["Use uma data."]})
    return value


def _row_dict(row: ReportRow) -> dict[str, Any]:
    return {name: getattr(row, name) for name in ReportRow.__dataclass_fields__}
