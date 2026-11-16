"""Test kit of the ledger for the apps that consume it (#545). Tests only.

``enable_ledger(organization)`` opens and activates the ledger and gives the organization's
administrators every action on its tables; ``prepare_book(actor)`` creates the default book with the
"Prestação de serviços" chart and binds extra keys. The consuming app then posts through its rule
with an actor that has the rule's action on the source record and no permission on the book.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


def enable_ledger(organization: Any) -> None:
    """The ledger available and active in ``organization``; the builtin admin role reads and manages books."""
    from skeleton.testing.applications import activate_app, grant_table_actions, open_app

    open_app("skeleton_ledger")
    activate_app("skeleton_ledger", organization.pk)
    grant_table_actions(
        organization.pk,
        role_code="organization_admin",
        table_key="skeleton_ledger.book",
        actions=("read", "create", "update", "delete", "restore", "manage_access"),
    )


def prepare_book(actor: Any, *, basis: str = "cash", bindings: dict[str, str] | None = None) -> Any:
    """Default book with the services chart; ``bindings`` maps extra keys to account codes."""
    from .service_api import LedgerService

    service = LedgerService()
    book = service.create_default_book(actor=actor, basis=basis)
    service.apply_chart(actor=actor, book_id=book.sysid)
    for key, code in (bindings or {}).items():
        service.bind_key(actor=actor, key=key, account_id=account_id(actor, code), book_id=book.sysid)
    return book


def account_id(actor: Any, code: str, *, book_id: UUID | None = None) -> UUID:
    from .service_api import LedgerService

    page = LedgerService().accounts(actor=actor, book_id=book_id, page_size=100)
    return next(item.sysid for item in page.items if item.code == code)
