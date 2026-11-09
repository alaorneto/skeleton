"""Run queued jobs synchronously in tests, with the worker's real contexts and retries."""

from __future__ import annotations

from django.utils import timezone

from skeleton.jobs.worker import Outcome, Worker

__all__ = ["run_pending_jobs"]


def run_pending_jobs(
    *, queues: tuple[str, ...] | None = None, limit: int = 1000, using: str = "default"
) -> list[Outcome]:
    """Execute every due job (retries included only once they are due) and return the outcomes.

    Call it outside any database context, after the enqueuing transaction committed.
    """
    from skeleton.jobs.models import Job

    if queues is None:
        queues = tuple(
            Job.all_objects.using(using)
            .filter(run_after__lte=timezone.now())
            .values_list("queue", flat=True)
            .distinct()
        ) or ("default",)
    worker = Worker(worker_id="test-worker", queues=queues, using=using)
    outcomes: list[Outcome] = []
    while len(outcomes) < limit:
        outcome = worker.run_once()
        if outcome is None:
            break
        outcomes.append(outcome)
    return outcomes
