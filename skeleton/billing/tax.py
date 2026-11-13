"""Extension point for taxes (#396): the core charges no tax by default.

A project that must add taxes registers one calculator in its ``AppConfig.ready``. It receives
the account and the drafted lines and returns the tax in the invoice's minor units. Fiscal
documents (NFS-e) stay outside the core: an in-process consumer of ``billing.invoice_paid``
can hand the paid invoice to an emitter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class TaxCalculator(Protocol):
    def tax(self, *, account: Any, currency: str, lines: Sequence[Any]) -> int: ...


class NoTax:
    def tax(self, *, account: Any, currency: str, lines: Sequence[Any]) -> int:
        return 0


_calculator: TaxCalculator = NoTax()


def register_tax_calculator(calculator: TaxCalculator) -> None:
    global _calculator
    if not isinstance(_calculator, NoTax) and _calculator is not calculator:
        raise ValueError("A tax calculator is already registered.")
    _calculator = calculator


def tax_calculator() -> TaxCalculator:
    return _calculator
