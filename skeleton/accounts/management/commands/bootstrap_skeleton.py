import getpass
import os

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from skeleton.accounts.services import bootstrap_identity


class Command(BaseCommand):
    help = 'Create an idempotent platform administrator and organization. Password is never a CLI argument.'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--organization', required=True)
        parser.add_argument('--name', required=True)
        parser.add_argument('--database', default='default')

    def handle(self, *args, **options):
        from skeleton.accounts.models import User
        email = options['email'].strip().lower()
        password = os.environ.get('SKELETON_BOOTSTRAP_PASSWORD')
        if not User.all_objects.using(options['database']).filter(email=email).exists() and not password:
            if not self.stdin.isatty() if hasattr(self, 'stdin') else not __import__('sys').stdin.isatty():
                raise CommandError('Set SKELETON_BOOTSTRAP_PASSWORD or use an interactive terminal.')
            password = getpass.getpass('Bootstrap password: ')
            if password != getpass.getpass('Confirm password: '):
                raise CommandError('Passwords do not match.')
        try:
            user, organization = bootstrap_identity(email=email, organization=options['organization'], name=options['name'], password=password, using=options['database'])
        except ValidationError as exc:
            raise CommandError('; '.join(exc.messages)) from exc
        self.stdout.write(self.style.SUCCESS(f'Administrator {user.pk}; organization {organization.pk}'))
