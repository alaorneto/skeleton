"""Display filters of the core templates: dates and times as DD/MM/AAAA HH:mm (#468).

``{{ value|moment }}`` renders ``<time datetime="ISO">15/11/2026 14:30</time>`` in the active
time zone (the person's, the organization's or the instance's, activated by the locale
middleware); ``|moment:"date"`` and ``|moment:"time"`` show only one part. Never print a date
raw in a template: the guard in ``tests/unit/test_template_dates.py`` refuses it.
"""

from __future__ import annotations

from datetime import date, datetime, time

from django import template
from django.utils import formats, timezone
from django.utils.html import format_html

register = template.Library()
EMPTY = "—"


@register.filter(is_safe=True)
def moment(value, kind="datetime"):
    """<time> with DD/MM/AAAA HH:mm in the active time zone; "—" for empty values."""
    if value is None or value == "":
        return EMPTY
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value  # Not a date: autoescaped by the template like any text.
    if not isinstance(value, (date, time)):
        return value
    iso = value.isoformat()  # The instant as stored; the text below is in the active time zone.
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        if kind == "date":
            text = formats.date_format(value, "DATE_FORMAT")
        elif kind == "time":
            text = formats.time_format(value, "TIME_FORMAT")
        else:
            text = formats.date_format(value, "DATETIME_FORMAT")
    elif isinstance(value, date):
        text = formats.date_format(value, "DATE_FORMAT")
    else:
        text = formats.time_format(value, "TIME_FORMAT")
    return format_html('<time datetime="{}">{}</time>', iso, text)
