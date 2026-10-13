import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from skeleton.system.retained_schema import export_schema, RetainedSchemaError


class Command(BaseCommand):
    help = 'Freeze an installed application schema for retained-data deployment checks.'

    def add_arguments(self, parser):
        parser.add_argument('--app-label', required=True)
        parser.add_argument('--output', type=Path, required=True)

    def handle(self, *args, **options):
        try:
            snapshot = export_schema(options['app_label'])
            path = options['output']
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(snapshot, stream, sort_keys=True, separators=(',', ':'))
                stream.write('\n')
        except (ValueError, OSError, RetainedSchemaError) as exc:
            raise CommandError(f'Schema export failed: {type(exc).__name__}') from None
        self.stdout.write(str(path))
