"""Enqueue periodic jobs window by window; safe to run on several hosts at once."""

import signal
import time

from django.core.management.base import BaseCommand

from skeleton.jobs.periodic import schedule_due
from skeleton.jobs.worker import maintenance_paused


class Command(BaseCommand):
    help = "Enfileira os jobs periódicos da janela atual; vários agendadores não duplicam execuções."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Agenda a janela atual e sai.")
        parser.add_argument("--interval", type=float, default=30.0, help="Segundos entre verificações.")
        parser.add_argument("--database", default="default")

    def handle(self, *args, once, interval, database, **options):
        self.stopping = False

        def stop(*_):
            self.stopping = True

        for name in ("SIGTERM", "SIGINT"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), stop)
        total = 0
        while not self.stopping:
            if not maintenance_paused():
                total += schedule_due(using=database)
            if once:
                break
            time.sleep(interval)
        self.stdout.write(f"Jobs periódicos criados: {total}.")
