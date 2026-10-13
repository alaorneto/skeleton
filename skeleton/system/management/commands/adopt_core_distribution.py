import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from skeleton.distribution import adopt_source_core, source_adoption_plan
from skeleton.system.packages import PackageError, validate_package


class Command(BaseCommand):
    help = "Compare a copied core with a known signed base; --apply moves only an equivalent core to backup."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--project-root", required=True)
        parser.add_argument("--base-release", required=True)
        parser.add_argument("--trust-store", required=True)
        parser.add_argument("--backup-root")
        parser.add_argument("--apply", action="store_true")

    def handle(self, **options):
        if options["apply"] and not options["backup_root"]:
            raise CommandError("--apply requires --backup-root outside the project.")
        try:
            trust = json.loads(Path(options["trust_store"]).read_text(encoding="utf-8"))
            base = validate_package(options["base_release"], trust)
            result = (adopt_source_core(options["project_root"], base, options["backup_root"]) if options["apply"]
                      else source_adoption_plan(options["project_root"], base))
        except (OSError, ValueError, PackageError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({key: value for key, value in result.items() if key != "files"}, sort_keys=True))
