"""Jobs and notification type of the transfer app (#182, #183)."""

from __future__ import annotations


def register_transfer_jobs() -> None:
    from datetime import time

    from django.utils.translation import gettext_lazy

    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type
    from skeleton.notifications.registry import NotificationType, register_notification_type

    from .exports import EXPORT_JOB, NOTIFY_JOB, READY_TYPE, notify_ready, run_export
    from .retention import RETENTION_JOB, clean_transfers

    # Runs as the requester: a revoked membership or permission fails closed in the job.
    register_job_type(
        JobType(
            EXPORT_JOB,
            run_export,
            {"transfer_id": "uuid"},
            run_as="actor",
            max_attempts=3,
            timeout_s=900,
            owner_app="sk_transfer",
        )
    )
    register_job_type(
        JobType(
            NOTIFY_JOB, notify_ready, {"transfer_id": "uuid"}, run_as="system", max_attempts=3, owner_app="sk_transfer"
        )
    )
    register_job_type(
        JobType(RETENTION_JOB, clean_transfers, {}, run_as="system", max_attempts=3, owner_app="sk_transfer")
    )
    register_periodic(RETENTION_JOB, RETENTION_JOB, daily_at=time(3, 30))
    register_notification_type(
        NotificationType(
            READY_TYPE,
            gettext_lazy("Arquivo pronto para baixar"),
            payload_fields=("transfer",),
            owner_app="sk_transfer",
        )
    )
