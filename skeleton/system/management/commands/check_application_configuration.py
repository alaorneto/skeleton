from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError

from skeleton.system.app_registry import all_apps, validate_configuration
from skeleton.system.models import AppInstallation
from skeleton.tenancy.context import system_context
from skeleton.tenancy.models import Organization


class Command(BaseCommand):
    help = "Validate installed application configuration in independent organization transactions."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        using = options["database"]
        manifests = {manifest.app_label: manifest for manifest in all_apps()}
        ids = list(Organization.objects.using(using).with_deleted().values_list("sysid", flat=True))
        checked = 0
        errors = []
        for identity in ids:
            with system_context(reason="check:application-configuration", organization_id=identity, using=using):
                for row in AppInstallation.objects.using(using).filter(app_label__in=manifests):
                    try:
                        validate_configuration(manifests[row.app_label], row.configuration)
                    except ValidationError:
                        errors.append(f"{row.app_label}: configuration {row.pk} requires a versioned migration.")
                    checked += 1
        if errors:
            raise CommandError("\n".join(errors))
        self.stdout.write(f"Application configuration valid: {checked} persisted installations.")
