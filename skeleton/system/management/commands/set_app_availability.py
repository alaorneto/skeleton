"""Development/CLI counterpart of the platform availability screens (#22).

Apps are closed by default: without an availability row nobody can use them, and
without an ``AppInstallation`` they are disabled. This command opens an app on a
development database or from an operator's shell; HTTP never reaches it.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from skeleton.system.app_registry import get_app, validate_configuration
from skeleton.tenancy.context import system_context

REASON = "cli:app-availability"


class Command(BaseCommand):
    help = "Set an installed app's availability (all/selected), grant organizations and optionally enable it there."

    def add_arguments(self, parser):
        parser.add_argument("app_label")
        parser.add_argument("--mode", choices=("all", "selected"), required=True)
        parser.add_argument("--grant", action="append", default=[], metavar="SLUG", help="Admit an organization.")
        parser.add_argument(
            "--enable", action="append", default=[], metavar="SLUG", help="Also activate the app in an organization."
        )
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        from skeleton.system.models import AppAvailability, AppInstallation, AppOrganizationGrant
        from skeleton.tenancy.models import Organization

        using, label = options["database"], options["app_label"]
        try:
            manifest = get_app(label)
        except KeyError:
            raise CommandError(f"Unknown installed app: {label}.") from None
        slugs = sorted(set(options["grant"]) | set(options["enable"]))
        with transaction.atomic(using=using), system_context(reason=REASON, using=using):
            organizations = {
                org.slug: org for org in Organization.objects.using(using).filter(slug__in=slugs, is_active=True)
            }
            unknown = sorted(set(slugs) - set(organizations))
            if unknown:
                raise CommandError(f"Unknown or inactive organizations: {', '.join(unknown)}.")
            row = AppAvailability.all_objects.using(using).select_for_update().filter(app_label=label).first()
            if row is None:
                row = AppAvailability(app_label=label, mode=options["mode"], upgrade_state=AppAvailability.UPGRADE_DONE)
                row.save(using=using)
            elif row.mode != options["mode"]:
                row.mode = options["mode"]
                row.save(using=using, update_fields={"mode"})
            for slug in sorted(set(options["grant"]) | (set(options["enable"]) if row.mode == "selected" else set())):
                grant = (
                    AppOrganizationGrant.all_objects.using(using)
                    .filter(availability=row, granted_organization=organizations[slug])
                    .first()
                )
                if grant is None:
                    AppOrganizationGrant(availability=row, granted_organization=organizations[slug]).save(using=using)
                elif grant.deleted_at is not None:
                    grant.restore(using=using)
        for slug in sorted(set(options["enable"])):
            organization = organizations[slug]
            # One transaction per organization, like every tenant write of a command.
            with (
                transaction.atomic(using=using),
                system_context(reason=REASON, organization_id=organization.pk, using=using),
            ):
                record = AppInstallation.objects.using(using).filter(app_label=label).first()
                if record is None:
                    AppInstallation(
                        organization=organization,
                        app_label=label,
                        enabled=True,
                        configuration=validate_configuration(manifest, {}),
                    ).save(using=using)
                elif not record.enabled:
                    record.enabled = True
                    record.save(using=using, update_fields={"enabled"})
        self.stdout.write(
            self.style.SUCCESS(f"{label}: availability {options['mode']}; enabled in {len(options['enable'])}.")
        )
