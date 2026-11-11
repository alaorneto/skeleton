"""Probe the job queue: exit 1 when the oldest due job waits too long or no worker answers."""

from django.core.management.base import BaseCommand, CommandError

from skeleton.jobs.operations import queue_health


class Command(BaseCommand):
    help = "Verifica o atraso da fila de jobs, os jobs esgotados e os workers vivos; sai com 1 se houver problema."

    def add_arguments(self, parser):
        parser.add_argument("--max-lag", type=float, default=300.0, help="Atraso máximo, em segundos, do job vencido.")
        parser.add_argument("--max-dead", type=int, default=None, help="Quantidade máxima de jobs esgotados.")
        parser.add_argument("--heartbeat", type=int, default=120, help="Segundos sem heartbeat para um worker morto.")
        parser.add_argument("--require-worker", action="store_true", help="Falha sem nenhum worker vivo.")

    def handle(self, *args, max_lag, max_dead, heartbeat, require_worker, **options):
        health = queue_health(heartbeat_s=heartbeat)
        self.stdout.write(
            f"due={health.due} lag={int(health.oldest_due_lag_s)}s dead={health.dead} "
            f"workers={health.live_workers} stale_workers={health.stale_workers}"
        )
        problems = health.problems(max_lag_s=max_lag, max_dead=max_dead, require_worker=require_worker)
        if problems:
            raise CommandError("; ".join(problems))
