"""Background jobs of the files app: quota recount (#61), antivirus and orphan cleanup (#62)."""

from __future__ import annotations


def register_file_jobs() -> None:
    from datetime import time

    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    from .quota import RECOUNT_JOB, recount

    register_job_type(JobType(RECOUNT_JOB, recount, {}, run_as="system", max_attempts=3, owner_app="sk_files"))
    register_periodic(RECOUNT_JOB, RECOUNT_JOB, daily_at=time(2, 15))
    from datetime import timedelta

    from .orphans import ORPHANS_JOB, clean_orphans
    from .scanning import SCAN_JOB, scan_attachment

    # A scanner that is down raises: the job retries with backoff and the file stays pending.
    register_job_type(
        JobType(
            SCAN_JOB,
            scan_attachment,
            {"attachment_id": "uuid"},
            run_as="system",
            max_attempts=10,
            backoff=60.0,
            timeout_s=300,
            owner_app="sk_files",
        )
    )
    register_job_type(JobType(ORPHANS_JOB, clean_orphans, {}, run_as="system", max_attempts=3, owner_app="sk_files"))
    register_periodic(ORPHANS_JOB, ORPHANS_JOB, every=timedelta(hours=6))
    from .purge import PURGE_JOB, purge_deleted

    # Deleted attachments lose their bytes after the retention window (#192).
    register_job_type(JobType(PURGE_JOB, purge_deleted, {}, run_as="system", max_attempts=3, owner_app="sk_files"))
    register_periodic(PURGE_JOB, PURGE_JOB, daily_at=time(3, 0))
