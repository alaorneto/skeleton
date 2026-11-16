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

from .models import Account, AccountBinding, Book, CostCenter, Entry, EntryLine
from .posting import LineDraft as PostingLine
from .registry import AccountKey, register_account_key

APP_LABEL = "skeleton_ledger"
__all__ = [
    "AccountInfo",
    "AccountKey",
    "AccountKeyInfo",
    "BookInfo",
    "CostCenterInfo",
    "EntryInfo",
    "EntryLineInfo",
    "LedgerService",
    "PostingLine",
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
    )


class LedgerService(ServiceAPI):
    """Books, chart of accounts, entries, titles and reports of the current organization."""

    app_label = APP_LABEL

    @service_method(read_only=True, since="0.6.0")
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

    @service_method(read_only=False, since="0.6.0")
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

    @service_method(read_only=True, since="0.6.0")
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

    @service_method(read_only=False, since="0.6.0")
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

    @service_method(read_only=False, since="0.6.0")
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

    @service_method(read_only=False, since="0.6.0")
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

    @service_method(read_only=False, since="0.6.0")
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

    @service_method(read_only=True, since="0.6.0")
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

    @service_method(read_only=False, since="0.6.0")
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

    @service_method(read_only=True, since="0.6.0")
    def cost_centers(self, *, actor: Any, book_id: UUID | None = None) -> tuple[CostCenterInfo, ...]:
        """Cost centers of a book (at most 500).

        Autorização: leitura do livro.
        Erros: NotFound.
        Efeitos: nenhum.
        """
        book = _readable_book(actor, book_id)
        return tuple(cost_center_info(row) for row in CostCenter.objects.filter(book=book)[:500])

    @service_method(read_only=False, since="0.6.0")
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

    @service_method(read_only=False, since="0.6.0")
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

    @service_method(read_only=False, since="0.6.0")
    def reverse(
        self, *, actor: Any, entry_id: UUID, date: date, reason: str = "", idempotency_key: str = ""
    ) -> EntryInfo:
        """Reverse an entry once: the inverse entry, linked by ``reverses``; a reversal is not reversible.

        Autorização: gestão do livro do lançamento; o de uma regra também pelo registro de origem (#545).
        Erros: NotFound; Forbidden; Conflict("already_reversed"); Conflict("period_locked");
        InvalidInput para estornar um estorno.
        Efeitos: grava o estorno selado na transação do chamador.
        """
        from . import posting

        entry = _entry(actor, entry_id, manage=True)
        return _post(
            lambda: posting.reverse_entry(
                entry=entry,
                date=date,
                reason=reason,
                actor_id=getattr(actor, "pk", None),
                idempotency_key=_key(idempotency_key, required=False),
            )
        )

    @service_method(read_only=True, since="0.6.0")
    def entry(self, *, actor: Any, entry_id: UUID) -> EntryInfo:
        """One sealed entry with its lines.

        Autorização: leitura do livro.
        Erros: NotFound para lançamento inexistente, ilegível ou de outra organização.
        Efeitos: nenhum.
        """
        return entry_info(_entry(actor, entry_id))

    @service_method(read_only=True, since="0.6.0")
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

    @service_method(read_only=False, since="0.6.0")
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
