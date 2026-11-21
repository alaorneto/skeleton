"""Datas digitadas pelo usuário nos filtros do relatório."""

from datetime import date


def parse_date(text, today=None):
    """Lê `dd/mm` ou `dd/mm/aaaa`."""
    today = today or date.today()
    parts = [int(part) for part in text.strip().split("/")]
    if len(parts) == 2:
        day, month = parts
        candidate = date(today.year, month, day)
        return candidate if candidate <= today else date(today.year - 1, month, day)
    day, month, year = parts
    if year < 100:
        year += 2000
    return date(year, month, day)
