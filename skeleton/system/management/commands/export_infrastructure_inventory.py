import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from skeleton.system.infrastructure_snapshot import export_inventory


class Command(BaseCommand):
    help = 'Export primitive frozen migration contracts for runtime verification without archive imports.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True, type=Path)

    def handle(self, *args, **options):
        try:
            value = export_inventory()
            path = options['output']
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, sort_keys=True, separators=(',', ':'))
                stream.write('\n')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CommandError(f'Infrastructure export failed: {type(exc).__name__}') from None
        self.stdout.write(str(path))
