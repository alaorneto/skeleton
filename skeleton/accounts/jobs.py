"""Background jobs of the identity app: cleanup of an anonymized identity in each organization (#191)."""

from __future__ import annotations


def register_account_jobs() -> None:
    from skeleton.jobs.registry import JobType, register_job_type

    from .erasure import ANONYMIZE_ORGANIZATION_JOB, anonymize_organization

    register_job_type(
        JobType(
            ANONYMIZE_ORGANIZATION_JOB,
            anonymize_organization,
            {"user": "uuid"},
            run_as="system",
            max_attempts=5,
            owner_app="sk_accounts",
        )
    )
