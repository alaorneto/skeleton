"""Timeline merge, order and keyset cursor (#185), with in-memory sources and no database."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError

from skeleton.web import timeline
from skeleton.web.timeline import TimelineItem, decode_cursor, encode_cursor, record_timeline

BASE = datetime(2026, 11, 12, 12, tzinfo=UTC)


def _source(kind, offsets):
    rows = [TimelineItem(kind, BASE - timedelta(minutes=offset), UUID(int=offset + 1)) for offset in offsets]

    def source(spec, record, *, user, before, limit):
        older = [row for row in rows if before is None or (row.occurred_at, row.sysid) < before]
        return sorted(older, key=lambda row: row.key, reverse=True)[:limit]

    return source


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(
        timeline, "_sources", {"audit": _source("audit", [0, 2, 4]), "attachment": _source("attachment", [1, 3])}
    )


def test_items_are_merged_newest_first_across_pages(sources):
    first, cursor = record_timeline(None, None, user=None, page_size=2)
    assert [(item.kind, item.sysid.int) for item in first] == [("audit", 1), ("attachment", 2)]
    second, cursor = record_timeline(None, None, user=None, cursor=cursor, page_size=2)
    assert [(item.kind, item.sysid.int) for item in second] == [("audit", 3), ("attachment", 4)]
    last, cursor = record_timeline(None, None, user=None, cursor=cursor, page_size=2)
    assert [item.sysid.int for item in last] == [5] and cursor is None


def test_cursor_round_trip_and_invalid_values(sources):
    item = TimelineItem("audit", BASE, UUID(int=9))
    assert decode_cursor(encode_cursor(item)) == (BASE, UUID(int=9))
    assert decode_cursor(None) is None and decode_cursor("") is None
    for bad in ("x", "bm90LWEtY3Vyc29y", encode_cursor(TimelineItem("a", BASE.replace(tzinfo=None), UUID(int=1)))):
        with pytest.raises(ValidationError):
            decode_cursor(bad)
    for size in (0, 101, "5"):
        with pytest.raises(ValidationError):
            record_timeline(None, None, user=None, page_size=size)


def test_sources_register_once(monkeypatch):
    monkeypatch.setattr(timeline, "_sources", {})
    source = _source("comment", [])
    timeline.register_timeline_source("comment", source)
    timeline.register_timeline_source("comment", source)
    with pytest.raises(ValueError):
        timeline.register_timeline_source("comment", _source("comment", []))
