"""Pages of the ledger (#542, #549): every read and write goes through ``LedgerService``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from skeleton.service_api import Forbidden, InvalidInput, NotFound
from skeleton.web.views import tenant_page

from .service_api import LedgerService

if TYPE_CHECKING:
    from uuid import UUID


@tenant_page()
def home(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    try:
        books = LedgerService().books(actor=request.user)
    except Forbidden:
        raise Http404 from None
    return render(
        request,
        "skeleton_ledger/home.html",
        {"organization_id": organization_id, "books": books, "has_default": any(book.is_default for book in books)},
    )


@require_POST
@tenant_page()
@transaction.atomic
def default_book(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    basis = request.POST.get("basis", "")
    try:
        LedgerService().create_default_book(actor=request.user, basis=basis)
    except (Forbidden, NotFound):
        raise Http404 from None
    except InvalidInput:
        messages.error(request, gettext("Escolha o regime do livro: caixa ou competência."))
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
