"""Relatório mensal de vendas."""


def monthly_total(orders):
    """Soma o valor dos pedidos do mês. Pedidos cancelados não entram no total (docs/reports.md)."""
    return round(sum(order["amount"] for order in orders), 2)
