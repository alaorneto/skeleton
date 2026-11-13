"""The digest job decides by the window it was scheduled for (#276)."""

from datetime import UTC, datetime

from skeleton.notifications.channels.digest import window_instant


def test_window_stamp_parses_to_the_utc_instant():
    instant = window_instant("20261116T080000Z")
    assert instant == datetime(2026, 11, 16, 8, 0, tzinfo=UTC) and instant.tzinfo is UTC
