from django.core.management.base import BaseCommand
from skeleton.accounts.providers import validate_provider_bindings


class Command(BaseCommand):
    help = 'Validate configured provider organizations, roles and historical subject bindings.'

    def add_arguments(self, parser):
        parser.add_argument('--database', default='default')

    def handle(self, *args, **options):
        validate_provider_bindings(using=options['database'])
        self.stdout.write(self.style.SUCCESS('SSO provider bindings are valid.'))
