"""Ledger services (#542): every write of the app goes through here."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

from skeleton.access.policies import has_table_permission
from skeleton.tenancy.context import current_context

from .models import Book

DEFAULT_NAME = "Livro principal"


def ensure_default_book(user: Any, *, basis: str = Book.Basis.CASH, currency: str = "BRL") -> Book:
    """The organization's default book, created once by whoever may create books (#542).

    Concurrent first visits race on the partial unique constraint; the loser reads the winner.
    """
    existing = Book.objects.filter(is_default=True).first()
    if existing is not None:
        return existing
    if not has_table_permission(Book, user=user, action="create"):
        raise PermissionDenied("Book table permission required.")
    book = Book(
        organization_id=current_context().organization_id,  # type: ignore[misc]  # Set: tenant context.
        owner_id=user.pk,
        visibility=Book.Visibility.PRIVATE,
        name=DEFAULT_NAME,
        currency=currency,
        basis=basis,
        is_default=True,
    )
    try:
        with transaction.atomic(using=current_context().using):
            book.save()
    except (IntegrityError, ValidationError):
        winner = Book.objects.filter(is_default=True).first()
        if winner is None:
            raise
        return winner
    return book


# --------------------------------------------------------------------------- generic gateway operations


def create_book_record(record: Book, *, user: Any, values: dict[str, Any]) -> None:
    """Books created through the table gateway are never the default one (only ``ensure_default_book``)."""
    record.is_default = False
    record.save()


def update_book_record(record: Book, *, user: Any, values: dict[str, Any]) -> None:
    for name, value in values.items():
        setattr(record, name, value)
    record.save(update_fields=set(values))


def delete_book_record(record: Book, *, user: Any) -> None:
    if record.is_default:
        raise ValidationError({"is_default": gettext("O livro padrão não pode ser excluído.")})
    record.delete()


def restore_book_record(record: Book, *, user: Any) -> None:
    record.restore()


def grant_member_access(*, actor: Any) -> tuple[str, ...]:
    """Organization admin action: read access to the books for the built-in ``member`` role."""
    from skeleton.service_api.access import AccessService

    return AccessService().grant_member_table_access(actor=actor, table_key="skeleton_ledger.book", actions=("read",))


def book_count(organization_id: Any) -> int:
    """State metric ``skeleton_ledger.books`` of the plan limits (#542)."""
    return Book.all_objects.filter(organization_id=organization_id, deleted_at__isnull=True).count()
