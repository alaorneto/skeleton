"""pt-BR display formats of the core: short numeric dates (DD/MM/AAAA HH:mm, #468).

Only the display formats live here; the rest (decimal separator, first day of the week, input
formats) comes from Django's own pt_BR module.
"""

DATE_FORMAT = "d/m/Y"
TIME_FORMAT = "H:i"
DATETIME_FORMAT = "d/m/Y H:i"
SHORT_DATE_FORMAT = "d/m/Y"
SHORT_DATETIME_FORMAT = "d/m/Y H:i"
