"""Which digests fire at a given local time (#55)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from skeleton.notifications.channels.digest import due

MONDAY = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def test_daily_digest_fires_at_its_local_hour_only():
    local = MONDAY.astimezone(ZoneInfo("America/Sao_Paulo"))
    assert local.hour == 9
    assert due("daily", 9, local)
    assert not due("daily", 12, local)  # 12:00 is the UTC hour, not the person's.
    assert not due("immediate", 9, local) and not due("off", 9, local)


def test_weekly_digest_fires_on_mondays():
    assert due("weekly", 12, MONDAY)
    assert not due("weekly", 12, MONDAY.replace(day=10))
    tokyo = MONDAY.replace(hour=20).astimezone(ZoneInfo("Asia/Tokyo"))  # Tuesday 05:00 in Tokyo.
    assert not due("weekly", 5, tokyo) and due("daily", 5, tokyo)
