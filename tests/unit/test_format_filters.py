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


def test_status_badge(subtests):
    from skeleton.web.templatetags.skeleton_format import status_badge

    with translation.override("pt-br"):
        cases = (
            ("succeeded", None, '<span class="badge badge-success badge-dot">Concluído</span>'),
            ("xyz", None, '<span class="badge badge-neutral badge-dot">xyz</span>'),
            ("dead", "Esgotado agora", '<span class="badge badge-danger badge-dot">Esgotado agora</span>'),
            ("completed", "flows", '<span class="badge badge-success badge-dot">Concluída</span>'),
            ("open", "invoice", '<span class="badge badge-warning badge-dot">Em aberto</span>'),
            ("<b>x</b>", None, '<span class="badge badge-neutral badge-dot">&lt;b&gt;x&lt;/b&gt;</span>'),
            ("ok", "<i>y</i>", '<span class="badge badge-neutral badge-dot">&lt;i&gt;y&lt;/i&gt;</span>'),
            (None, None, "—"),
        )
        for code, label, expected in cases:
            with subtests.test(code=code, label=label):
                assert str(status_badge(code, label)) == expected


def test_every_core_state_code_has_a_badge():
    from django.apps import apps
    from django.db import models

    from skeleton.web.templatetags.skeleton_format import STATUS, TONES

    assert {tone for _, tone in STATUS.values()} <= TONES
    known = {key.rsplit(":", 1)[-1] for key in STATUS}
    missing = set()
    for model in apps.get_models():
        if not model.__module__.startswith("skeleton."):
            continue
        for name in dir(model):
            choices = getattr(model, name, None)
            if (
                isinstance(choices, type)
                and issubclass(choices, models.TextChoices)
                and ("Status" in name or "State" in name)
            ):
                missing |= {f"{model._meta.label}.{name}:{value}" for value in choices.values if value not in known}
    assert not missing, sorted(missing)
    assert all(key in STATUS for key in {code for code in known})  # Every domain code also has a plain entry.


def test_list_columns_have_a_kind_and_long_text_is_cut():
    from types import SimpleNamespace

    from skeleton.jobs.models import Job
    from skeleton.web.presentation import LIST_TEXT_LIMIT, _column_kind, _list_text

    spec = SimpleNamespace(model=Job)
    kinds = {name: _column_kind(spec, name) for name in ("attempts", "run_after", "job_type", "state", "__label__")}
    assert kinds == {"attempts": "num", "run_after": "date", "job_type": "text", "state": "text", "__label__": "text"}
    assert _list_text("x" * LIST_TEXT_LIMIT) == "x" * LIST_TEXT_LIMIT
    assert _list_text("y" * 400) == "y" * LIST_TEXT_LIMIT + "…"
