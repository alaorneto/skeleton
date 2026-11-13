"""Background jobs of the notifications app: fan-out, email, push, digest and retention (#278).

Registered in ``AppConfig.ready()``; the types, payloads, limits and periodic schedules are
the ones ``services.py`` used to register.
"""

from __future__ import annotations

from .services import FANOUT_JOB


def register_notification_jobs() -> None:
    from skeleton.jobs.registry import JobType, register_job_type

    from .fanout import fan_out

    register_job_type(
        JobType(
            FANOUT_JOB,
            fan_out,
            {"notification_id": "uuid", "offset": "int"},
            run_as="system",
            owner_app="sk_notifications",
        )
    )
    from .channels.delivery import EMAIL_JOB
    from .channels.email import send_email

    register_job_type(
        JobType(
            EMAIL_JOB,
            send_email,
            {"delivery_id": "uuid"},
            run_as="system",
            # Crashes only: send failures never raise and follow RETRY_SCHEDULE through new jobs.
            max_attempts=3,
            backoff=60.0,
            timeout_s=120,
            owner_app="sk_notifications",
        )
    )
    from .channels.delivery import PUSH_JOB
    from .channels.push import send_push

    register_job_type(
        JobType(
            PUSH_JOB,
            send_push,
            {"delivery_id": "uuid"},
            run_as="system",
            # Crashes only: push outcomes never raise and follow RETRY_SCHEDULE through new jobs.
            max_attempts=3,
            backoff=60.0,
            timeout_s=120,
            owner_app="sk_notifications",
        )
    )
    from datetime import timedelta

    from skeleton.jobs.periodic import register_periodic

    from .channels.digest import DIGEST_JOB, run_digest

    register_job_type(
        JobType(
            DIGEST_JOB, run_digest, {"window": "str"}, run_as="system", max_attempts=3, owner_app="sk_notifications"
        )
    )
    register_periodic(DIGEST_JOB, DIGEST_JOB, every=timedelta(hours=1))
    from datetime import time

    from .retention import RETENTION_JOB, purge_expired_notifications

    register_job_type(
        JobType(
            RETENTION_JOB,
            purge_expired_notifications,
            {},
            run_as="system",
            max_attempts=3,
            owner_app="sk_notifications",
        )
    )
    register_periodic(RETENTION_JOB, RETENTION_JOB, daily_at=time(3, 30))
