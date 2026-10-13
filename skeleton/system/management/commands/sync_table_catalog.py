from django.core.management.base import BaseCommand
from skeleton.system.synchronization import sync_table_catalog


class Command(BaseCommand):
    help = "Reconcile physical table/column metadata without runtime DDL."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        result = sync_table_catalog(using=options["database"])
        self.stdout.write(self.style.SUCCESS(f"Catalog: {result['tables']} tables, {result['fields']} fields."))
