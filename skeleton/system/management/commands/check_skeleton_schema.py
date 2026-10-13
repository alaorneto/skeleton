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
        self.stdout.write(self.style.SUCCESS("Skeleton schema security inventory is valid."))
