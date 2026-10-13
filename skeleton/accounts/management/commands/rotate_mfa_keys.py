from copy import deepcopy

from cryptography.fernet import InvalidToken
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from skeleton.accounts.mfa import cipher
from skeleton.tenancy.context import system_context


class Command(BaseCommand):
    help = 'Reencrypt supported allauth MFA secrets using the first MFA_ENCRYPTION_KEYS key.'

    def add_arguments(self, parser):
        parser.add_argument('--database', default='default')

    def handle(self, *args, **options):
        from allauth.mfa.models import Authenticator
        using = options['database']
        encryption = cipher()
        with system_context(reason='mfa:key-rotation', using=using), transaction.atomic(using=using):
            authenticators = list(Authenticator.objects.using(using).select_for_update().order_by('pk'))
            updates = []
            # Validate every row before the first mutation. Unknown upstream layouts fail closed.
            for authenticator in authenticators:
                data = deepcopy(authenticator.data)
                if authenticator.type == Authenticator.Type.TOTP:
                    if set(data) != {'secret'} or not isinstance(data['secret'], str):
                        raise CommandError(f'Unknown TOTP data layout for authenticator {authenticator.pk}.')
                    paths = ['secret']
                elif authenticator.type == Authenticator.Type.RECOVERY_CODES:
                    if 'seed' not in data or set(data) - {'seed', 'used_mask', 'viewed_at', 'migrated_codes'} or not isinstance(data['seed'], str):
                        raise CommandError(f'Unknown recovery-code data layout for authenticator {authenticator.pk}.')
                    paths = ['seed']
                    if 'migrated_codes' in data and not isinstance(data['migrated_codes'], list):
                        raise CommandError(f'Unknown migrated recovery-code layout for authenticator {authenticator.pk}.')
                else:
                    raise CommandError(f'Unsupported authenticator type for {authenticator.pk}.')
                try:
                    for key in paths:
                        data[key] = encryption.rotate(data[key].encode()).decode()
                    if 'migrated_codes' in data:
                        data['migrated_codes'] = [encryption.rotate(value.encode()).decode() for value in data['migrated_codes']]
                except (InvalidToken, AttributeError, TypeError) as exc:
                    raise CommandError(f'Unable to decrypt authenticator {authenticator.pk}; retain all old keys.') from exc
                updates.append((authenticator, data))
            for authenticator, data in updates:
                authenticator.data = data
                authenticator.save(using=using, update_fields=['data'])
            self.stdout.write(f'Rotated {len(updates)} authenticators.')
