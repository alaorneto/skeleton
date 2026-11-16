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

from .models import Book

APP_LABEL = "skeleton_ledger"


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
