from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Re-resolve the TXT challenge of every verified email domain (#28) and custom domain (#488). DNS runs outside the "
        "transactions, one transaction per organization; three failures in a row mark the domain "
        "failed and drop its claim. Schedule it with cron or a systemd timer."
    )

    def add_arguments(self, parser):
        parser.add_argument("--recheck", action="store_true", help="Recheck every claimed domain.")

    def handle(self, *args, **options):
        from skeleton.tenancy.domains import recheck
        from skeleton.tenancy.site_domains import recheck_site_domains

        if not options["recheck"]:
            raise CommandError("Use --recheck.")
        report = recheck_site_domains(report=recheck())
        self.stdout.write(f"checked={report.checked} failures={report.failures} revoked={report.revoked}")
