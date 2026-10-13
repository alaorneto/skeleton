"""Django wrapper for the same standalone, settings-free package utilities."""
import json
from django.core.management.base import BaseCommand, CommandError
from skeleton.system.packages import PackageError, configure_parser, run_command


class Command(BaseCommand):
    help = 'Initialize, build, validate, inspect and sign app/core artifacts and release indexes.'
    requires_system_checks = []

    def add_arguments(self, parser):
        configure_parser(parser)

    def handle(self, *args, **options):
        # Django's global --version flag and init's component --version share a
        # destination; argparse passes the subcommand value to this handler.
        try:
            result = run_command(options)
        except (PackageError, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
