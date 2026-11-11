"""Mirror the first-party opt-in (SKELETON_BUNDLED_APPS/RETAINED) into the deployment state (#67)."""

from django.core.management.base import BaseCommand

from skeleton.system.bundled import sync_bundled_components


class Command(BaseCommand):
    help = "Registra as apps first-party instaladas ou retiradas; roda também depois de cada migrate."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")

    def handle(self, *args, database, **options):
        report = sync_bundled_components(using=database)
        for key, label in (("installed", "instaladas"), ("reinstalled", "reinstaladas"), ("retained", "retidas")):
            self.stdout.write(f"{label}: {', '.join(report[key]) or '—'}")
