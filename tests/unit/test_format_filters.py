"""Dates and times as DD/MM/AAAA HH:mm in the active time zone (#468)."""

from datetime import UTC, date, datetime, time

from django.template import Context, Template
from django.utils import formats, timezone, translation

from skeleton.web.templatetags.skeleton_format import moment

INSTANT = datetime(2026, 11, 15, 17, 30, tzinfo=UTC)


def test_core_formats_are_the_short_numeric_ones():
    with translation.override("pt-br"):
        assert formats.date_format(datetime(2026, 11, 15, 14, 30), "DATETIME_FORMAT") == "15/11/2026 14:30"
        assert formats.date_format(date(2026, 11, 15), "DATE_FORMAT") == "15/11/2026"
        assert formats.time_format(time(14, 30), "TIME_FORMAT") == "14:30"
        assert formats.get_format("DECIMAL_SEPARATOR") == ","  # Still Django's own pt_BR module.


def test_moment_in_the_active_time_zone(subtests):
    with translation.override("pt-br"), timezone.override("America/Sao_Paulo"):
        cases = (
            (INSTANT, {}, '<time datetime="2026-11-15T17:30:00+00:00">15/11/2026 14:30</time>'),
            (INSTANT, {"kind": "date"}, '<time datetime="2026-11-15T17:30:00+00:00">15/11/2026</time>'),
            (INSTANT, {"kind": "time"}, '<time datetime="2026-11-15T17:30:00+00:00">14:30</time>'),
            (datetime(2026, 11, 15, 9, 5), {}, '<time datetime="2026-11-15T09:05:00">15/11/2026 09:05</time>'),
            (date(2026, 11, 15), {}, '<time datetime="2026-11-15">15/11/2026</time>'),
            (time(8, 0), {}, '<time datetime="08:00:00">08:00</time>'),
            ("2026-11-15T17:30:00+00:00", {}, '<time datetime="2026-11-15T17:30:00+00:00">15/11/2026 14:30</time>'),
            (None, {}, "—"),
            ("", {}, "—"),
        )
        for value, kwargs, expected in cases:
            with subtests.test(value=value, **kwargs):
                assert str(moment(value, **kwargs)) == expected


def test_text_that_is_not_a_date_is_escaped():
    with translation.override("pt-br"):
        rendered = Template("{% load skeleton_format %}{{ value|moment }}").render(
            Context({"value": "<script>alert(1)</script>"})
        )
    assert rendered == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_moment_in_a_template():
    with translation.override("pt-br"), timezone.override("America/Sao_Paulo"):
        rendered = Template("{% load skeleton_format %}{{ a|moment }} {{ b|moment }}").render(
            Context({"a": INSTANT, "b": None})
        )
    assert rendered == '<time datetime="2026-11-15T17:30:00+00:00">15/11/2026 14:30</time> —'


def test_record_presentation_uses_the_short_formats():
    from django.db import models

    from skeleton.web.presentation import _value

    with translation.override("pt-br"), timezone.override("America/Sao_Paulo"):
        assert _value(models.DateTimeField(), INSTANT) == "15/11/2026 14:30"
        assert _value(models.DateField(), date(2026, 11, 15)) == "15/11/2026"
        assert _value(models.TimeField(), time(14, 30)) == "14:30"
        assert _value(models.DateTimeField(), None) == "—"
