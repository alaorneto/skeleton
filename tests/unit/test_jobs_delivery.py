"""The shared retry schedule and the channel of each delivery job (#346)."""

from datetime import timedelta

import pytest

from skeleton.jobs.delivery import RetrySchedule


def test_retry_schedule_is_indexed_by_attempts_already_made():
    schedule = RetrySchedule((timedelta(minutes=1), timedelta(minutes=5)))
    assert schedule.delay_after(1) == timedelta(minutes=1)
    assert schedule.delay_after(2) == timedelta(minutes=5)
    assert schedule.delay_after(3) is None and schedule.delay_after(0) is None
    assert schedule.max_attempts == 3


@pytest.mark.parametrize("attempt", range(0, 10))
def test_pipelines_keep_their_schedules(attempt):
    from skeleton.integrations.webhooks import delivery as webhooks
    from skeleton.notifications.channels import delivery as notifications

    old_webhooks = webhooks.RETRY_SCHEDULE[attempt - 1] if 1 <= attempt <= len(webhooks.RETRY_SCHEDULE) else None
    assert webhooks.retry_delay(attempt) == old_webhooks
    assert notifications.MAX_ATTEMPTS == len(notifications.RETRY_SCHEDULE) + 1 == 5


def test_metric_channels_come_from_the_job_registry():
    from skeleton.observability.metrics import _job_labels

    _, _, channels = _job_labels()
    assert channels == {"webhooks.deliver": "webhook", "notifications.email": "email", "notifications.push": "push"}
