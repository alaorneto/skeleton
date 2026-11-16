"""Authorization of the ledger (#542): everything derives from the book.

- Reading a book (and its chart, entries, titles and reports) needs ``read`` on the ``Book``.
- Managing a book (accounts, bindings, manual entries, the period lock) needs ``update`` on it:
  its owner, the organization administrator or a record grant, by the rules of ``SecuredRecord``.
- An automatic entry or a title with a source record is authorized by that record instead (#545):
  whoever settles an appointment posts without reading the book.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from skeleton.access.policies import authorized_queryset

from .models import Book

TABLE_KEY = "skeleton_ledger.book"


def readable_books(user: Any) -> QuerySet[Book]:
    return authorized_queryset(Book, user=user, action="read")


def writable_books(user: Any) -> QuerySet[Book]:
    return authorized_queryset(Book, user=user, action="update")


def can_manage(user: Any, book: Book) -> bool:
    return writable_books(user).filter(pk=book.pk).exists()
