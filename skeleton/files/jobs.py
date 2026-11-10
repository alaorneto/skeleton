"""Background jobs of the files app: quota recount (#61)."""

from __future__ import annotations


def register_file_jobs() -> None:
    from datetime import time

    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    from .quota import RECOUNT_JOB, recount

    register_job_type(JobType(RECOUNT_JOB, recount, {}, run_as="system", max_attempts=3, owner_app="sk_files"))
    register_periodic(RECOUNT_JOB, RECOUNT_JOB, daily_at=time(2, 15))
