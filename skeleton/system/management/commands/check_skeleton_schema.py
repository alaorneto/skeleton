from django.core.management.base import BaseCommand, CommandError

from skeleton.system.checks import check_deployed_schema


class Command(BaseCommand):
    help = "Inspect the exact deployed record security inventory after migrations."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        errors = check_deployed_schema(using=options["database"])
        if errors:
            raise CommandError("Skeleton schema is not protected:\n" + "\n".join(errors))
        from skeleton.system.activation_upgrade import apps_without_availability
        from skeleton.tenancy.context import system_context

        with system_context(reason="check:app-availability", using=options["database"]):
            closed = apps_without_availability(using=options["database"])
        for label in closed:
            # A warning, not a failure: closed is the safe default, but usually not the intent.
            self.stderr.write(
                self.style.WARNING(
                    f"Warning: installed app {label} has no availability row and is closed to every organization; "
                    "run upgrade_app_activation or set_app_availability."
                )
            )
        self.stdout.write(self.style.SUCCESS("Skeleton schema security inventory is valid."))
