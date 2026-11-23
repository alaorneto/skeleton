from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from skeleton.tenancy.context import command_context

REASON_LIMIT = 120


class Command(BaseCommand):
    help = (
        "Emergency reset of the MFA of an identity that lost its authenticator and recovery codes (#736), "
        "for when no other platform administrator can reset it in /platform/. Without --confirm, only reports."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Email of the identity.")
        parser.add_argument(
            "--reason", required=True, help="Why the MFA is reset (ticket, request); goes to the audit."
        )
        parser.add_argument("--confirm", action="store_true", help="Remove the authenticators and end every session.")
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        from skeleton.accounts.models import AccountType, User
        from skeleton.accounts.services import mfa_methods, normalized_email, reset_mfa

        reason = " ".join(options["reason"].split())[:REASON_LIMIT]
        if not reason:
            raise CommandError("--reason is required.")
        using = options["database"]
        try:
            email = normalized_email(options["email"])
        except ValidationError as exc:
            raise CommandError("Invalid email.") from exc
        with command_context(reason=f"mfa:reset {reason}", using=using):
            user = User.all_objects.using(using).filter(email=email, account_type=AccountType.HUMAN).first()
            if user is None:
                raise CommandError("No human identity with this email.")
            if not options["confirm"]:
                methods = mfa_methods(user, using=using)
                self.stdout.write(
                    f"Identity {user.pk}: MFA types {', '.join(methods) or 'none'}. "
                    "Nothing changed; run again with --confirm to reset."
                )
                return
            try:
                removed = reset_mfa(user, actor=None, expected_revision=None, using=using)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Identity {user.pk}: {removed} authenticator(s) removed; sessions revoked. "
                "The person enrolls MFA again on the next login."
            )
        )
