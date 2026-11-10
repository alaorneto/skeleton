from django.core.management.base import BaseCommand

from skeleton.system.activation_upgrade import upgrade_app_activation


class Command(BaseCommand):
    help = (
        "Keep the apps organizations were using when availability and activation become closed by default "
        "(idempotent and resumable; one transaction per organization)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List what would change without writing.")
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        report = upgrade_app_activation(dry_run=dry_run, using=options["database"])
        prefix = "Would create" if dry_run else "Created"
        for label in report.created_availability:
            self.stdout.write(f"{prefix} availability all for {label}.")
        for label in report.marked_pending:
            self.stdout.write(f"{'Would mark' if dry_run else 'Marked'} existing availability of {label} for upgrade.")
        for organization_id, labels in sorted(report.activations.items(), key=lambda item: str(item[0])):
            # Identifiers and labels only: no organization names or configuration values.
            self.stdout.write(f"{prefix} activation in organization {organization_id}: {', '.join(labels)}.")
        verb = "Would complete" if dry_run else "Completed"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} app activation upgrade: {len(report.completed)} apps, "
                f"{report.activation_count} activations in {len(report.activations)} organizations."
            )
        )
