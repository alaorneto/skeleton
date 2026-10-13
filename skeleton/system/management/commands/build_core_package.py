from django.core.management.base import BaseCommand, CommandError

from skeleton.distribution import build_core_release
from skeleton.system.packages import PackageError


class Command(BaseCommand):
    help = "Build a signed core release from a core-only wheel and canonical manifest (offline)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--wheel", required=True)
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--private-key", required=True)
        parser.add_argument("--dependency-wheel", action="append", default=[])

    def handle(self, **options):
        try:
            output = build_core_release(options["wheel"], options["manifest"], options["output"],
                                        options["private_key"], wheels=options["dependency_wheel"])
        except (OSError, ValueError, PackageError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(str(output))
