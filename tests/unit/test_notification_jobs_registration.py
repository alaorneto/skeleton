"""The notification jobs keep their codes, payloads and limits after moving to their module (#278)."""

from skeleton.jobs.periodic import all_periodic
from skeleton.jobs.registry import get_job_type


def test_notification_job_types_and_periodics_are_registered():
    expected = {
        "notifications.fanout": ({"notification_id", "offset"}, None),
        "notifications.email": ({"delivery_id"}, 120),
        "notifications.push": ({"delivery_id"}, 120),
        "notifications.digest": ({"window"}, None),
        "notifications.retention": (set(), None),
    }
    for code, (payload, timeout) in expected.items():
        kind = get_job_type(code)
        assert set(kind.payload_schema) == payload, code
        assert kind.owner_app == "sk_notifications" and kind.run_as == "system", code
        if timeout is not None:
            assert (kind.timeout_s, kind.max_attempts) == (timeout, 3), code
    assert get_job_type("notifications.digest").max_attempts == 3
    periodic = {job.code: job for job in all_periodic()}
    assert periodic["notifications.digest"].job_type == "notifications.digest"
    assert periodic["notifications.retention"].job_type == "notifications.retention"
