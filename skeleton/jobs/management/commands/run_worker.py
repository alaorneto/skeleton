"""Run the job worker: one context per job, retries with backoff, graceful SIGTERM."""

import signal

from django.core.management.base import BaseCommand

from skeleton.jobs.worker import Worker


class Command(BaseCommand):
    help = "Executa jobs da fila; SIGTERM termina o job atual e sai sem reivindicar outro."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Processa os jobs vencidos e sai.")
        parser.add_argument("--queues", default="default", help="Filas separadas por vírgula.")
        parser.add_argument("--max-jobs", type=int, default=None)
        parser.add_argument("--worker-id", default=None)
        parser.add_argument("--database", default="default")

    def handle(self, *args, once, queues, max_jobs, worker_id, database, **options):
        worker = Worker(
            worker_id=worker_id,
            queues=tuple(name.strip() for name in queues.split(",") if name.strip()),
            using=database,
        )
        for name in ("SIGTERM", "SIGINT"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), worker.stop)
        outcomes = worker.run(once=once, max_jobs=max_jobs)
        self.stdout.write(f"Jobs processados: {len(outcomes)}.")
