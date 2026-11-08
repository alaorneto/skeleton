"""Scaffold a project application that already follows the record contract."""

from django.core.management.base import BaseCommand, CommandError

from skeleton.system.app_scaffold import build_scaffold, create_application, next_steps
from skeleton.system.packages import PackageError


class Command(BaseCommand):
    help = (
        "Create applications/<package>/ with AppConfig, SecuredRecord model, TableSpec, URLs, views, "
        "template, docs and canonical manifest. Never edits settings or overwrites a directory."
    )
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("app_label", help="Unique Django app label, e.g. project_tasks.")
        parser.add_argument("--app-id", required=True, help="Stable component ID, e.g. com.example.tasks.")
        parser.add_argument("--publisher", required=True, help="Publisher ID used to sign releases.")
        parser.add_argument("--model", default="Item", help="CamelCase SecuredRecord model name (default: Item).")
        parser.add_argument("--package-name", help="Python package under applications/ (default: app_label).")
        parser.add_argument("--name", dest="display_name", help="Display name (default: app_label).")
        parser.add_argument("--directory", default="applications", help="Parent directory (default: applications).")

    def handle(self, *args, **options):
        try:
            scaffold = build_scaffold(
                app_label=options["app_label"],
                app_id=options["app_id"],
                publisher=options["publisher"],
                model=options["model"],
                package_name=options["package_name"],
                verbose_name=options["display_name"],
            )
            target, files = create_application(scaffold, options["directory"])
        except (PackageError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Created {target}"))
        for name in files:
            self.stdout.write(f"  {name}")
        self.stdout.write("\nPróximos passos:")
        for number, step in enumerate(next_steps(scaffold), start=1):
            self.stdout.write(f"  {number}. {step}")
