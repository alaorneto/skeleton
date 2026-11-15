"""``manage.py maintenance status|on|off``: the administrative maintenance mode (#446).

Only the instance SQLite is touched, never PostgreSQL, and no system check runs. The mode can
therefore be left with the database down: ``docker compose exec web python manage.py maintenance off``.
Every running process sees the change within a few seconds, without a restart.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from skeleton.common.logging import security_event
from skeleton.instance.maintenance import MaintenanceError, enter, leave, read_state, store_path
from skeleton.instance.store import InstanceStore, InstanceStoreError

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value):
    match = _DURATION.match(value.strip())
    if not match or int(match.group(1)) == 0:
        raise CommandError("Duração inválida: use um inteiro positivo com s, m, h ou d (por exemplo 90m ou 2h).")
    return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


def parse_until(value):
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise CommandError("Término inválido: use ISO 8601 com fuso, por exemplo 2026-11-14T03:00-03:00.") from exc
    if parsed.tzinfo is None:
        raise CommandError("Término sem fuso horário: informe o deslocamento, por exemplo -03:00 ou Z.")
    return parsed


class Command(BaseCommand):
    help = "Show, enter or leave the administrative maintenance mode (instance SQLite only, no database)."
    requires_system_checks: list[str] = []

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        actions.add_parser("status", help="Current state of both maintenance modes.")
        on = actions.add_parser("on", help="Enter the mode, or change the message or end of the ongoing one.")
        on.add_argument("--message", default="", help="Plain text shown to everyone (500 characters at most).")
        end = on.add_mutually_exclusive_group()
        end.add_argument("--until", help="Scheduled end, ISO 8601 with time zone; the mode then ends by itself.")
        end.add_argument("--for", dest="duration", help="Scheduled end from now: 90m, 2h, 1d...")
        on.add_argument("--keep-jobs", action="store_true", help="Do not pause the job worker and the scheduler.")
        actions.add_parser("off", help="Leave the mode.")

    def handle(self, *args, action, **options):
        path = store_path()
        if path is None:
            raise CommandError("SKELETON_MAINTENANCE_STORE não está definido.")
        store = InstanceStore(path)
        try:
            getattr(self, f"_{action}")(store, **options)
        except InstanceStoreError as exc:
            raise CommandError(str(exc)) from exc

    def _status(self, store, **options):
        state = read_state(store)
        now = datetime.now(UTC)
        if state.is_active(now):
            label = "ativa"
        elif state.expired(now):
            label = "encerrada pelo término agendado"
        else:
            label = "inativa"
        self.stdout.write(f"Manutenção administrativa: {label}")
        if state.active:
            self.stdout.write(f"Mensagem: {state.message or '(padrão)'}")
            self.stdout.write(f"Início: {state.started_at.isoformat() if state.started_at else '-'}")
            self.stdout.write(
                f"Término: {state.ends_at.isoformat() if state.ends_at else 'sem horário (saída manual)'}"
            )
            self.stdout.write(f"Jobs pausados: {'sim' if state.pause_jobs else 'não'}")
            self.stdout.write(f"Origem: {state.source}")
        marker = getattr(settings, "SKELETON_MAINTENANCE_MARKER", None)
        deploying = bool(marker and Path(marker).exists())
        self.stdout.write(f"Manutenção do deploy: {'ativa (só o agente encerra)' if deploying else 'inativa'}")

    def _on(self, store, *, message, until, duration, keep_jobs, **options):
        now = datetime.now(UTC)
        ends_at = parse_until(until) if until else (now + parse_duration(duration) if duration else None)
        try:
            state = enter(
                store, message=message, ends_at=ends_at, pause_jobs=not keep_jobs, source="cli:maintenance-on"
            )
        except MaintenanceError as exc:
            raise CommandError(str(exc)) from exc
        security_event("instance.maintenance_entered", reason_code="cli", source="cli:maintenance-on")
        end = state.ends_at.isoformat() if state.ends_at else "sem horário"
        self.stdout.write(f"Manutenção ativa (término: {end}; jobs {'pausados' if state.pause_jobs else 'ativos'}).")

    def _off(self, store, **options):
        if leave(store, source="cli:maintenance-off"):
            security_event("instance.maintenance_exited", reason_code="cli", source="cli:maintenance-off")
            self.stdout.write("Manutenção encerrada.")
        else:
            self.stdout.write("A manutenção já estava inativa.")
