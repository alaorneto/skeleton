"""Billing jobs (#396): one hourly cycle per organization.

``billing.cycle`` runs in ``system_context`` of each active organization: it gives an
organization without a subscription the default plan (an instance that turned billing on
later), closes every period that ended (one invoice per period) and runs the dunning (#399). The periodic schedule is
registered only with billing on, so an installation that does not sell the platform queues
nothing.
"""

from __future__ import annotations

from datetime import time, timedelta

CYCLE_JOB = "billing.cycle"
USAGE_JOB = "billing.usage_snapshot"
MAX_PERIODS_PER_RUN = 24


def cycle(**payload: object) -> None:
    from . import engine

    if not engine.billing_enabled():
        return
    if engine.current_subscription() is None and engine.account() is None:
        engine.start_default_subscription()
    for _ in range(MAX_PERIODS_PER_RUN):
        before = engine.current_subscription()
        if before is None:
            break
        engine.renew_due()
        after = engine.current_subscription()
        if after is None or (after.pk == before.pk and after.current_period_end == before.current_period_end):
            break
    from .dunning import run

    run()


def register_billing_jobs(*, enabled: bool) -> None:
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    from .quota import snapshot_job

    register_job_type(
        JobType(CYCLE_JOB, cycle, {"window": "str"}, run_as="system", max_attempts=5, owner_app="sk_billing")
    )
    register_job_type(
        JobType(USAGE_JOB, snapshot_job, {"window": "str"}, run_as="system", max_attempts=3, owner_app="sk_billing")
    )
    if enabled:
        register_periodic("billing-cycle", CYCLE_JOB, every=timedelta(hours=1))
        # Late in the UTC day, so the day's snapshot exists before a midnight renewal.
        register_periodic("billing-usage", USAGE_JOB, daily_at=time(23, 50))
