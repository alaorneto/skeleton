"""Offline rotation of identity provider secrets to the first IDENTITY_PROVIDER_ENCRYPTION_KEYS key."""

from cryptography.fernet import InvalidToken
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from skeleton.common.crypto import IDENTITY_PROVIDER_KEYS, encrypted_fields, multifernet
from skeleton.tenancy.context import system_context


class Command(BaseCommand):
    help = (
        "Reencrypt identity provider secrets with the first IDENTITY_PROVIDER_ENCRYPTION_KEYS key. "
        "Run offline with the new key first and every old key after it; prints only counts and IDs."
    )

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")

    def handle(self, *args, database, **options):
        multifernet(IDENTITY_PROVIDER_KEYS, purpose="identity provider")  # Fail before reading any row.
        rotated = []
        with system_context(reason="idp:key-rotation", using=database), transaction.atomic(using=database):
            for model, field in encrypted_fields(IDENTITY_PROVIDER_KEYS):
                rows = list(
                    model._base_manager.using(database)
                    .select_for_update()
                    .filter(**{f"{field.attname}__isnull": False})
                    .order_by("pk")
                )
                updates = []
                # Decrypt every row before the first write: a missing old key changes nothing.
                for row in rows:
                    try:
                        updates.append((row.pk, field.rotate(getattr(row, field.attname))))
                    except InvalidToken as exc:
                        raise CommandError(
                            f"Unable to decrypt {model._meta.label} {row.pk}; retain all old keys."
                        ) from exc
                for pk, value in updates:
                    model._base_manager.using(database).filter(pk=pk).update(**{field.attname: value})
                rotated.append((model._meta.label, field.name, [str(pk) for pk, _ in updates]))
        for label, name, ids in rotated:
            self.stdout.write(f"{label}.{name}: rotated {len(ids)} ({', '.join(ids) or 'none'}).")
        self.stdout.write(f"Rotated {sum(len(ids) for _, _, ids in rotated)} identity provider secrets.")
