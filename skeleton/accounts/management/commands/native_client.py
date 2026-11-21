"""Register, list and revoke the native clients of the OIDC provider (#603).

``register`` creates the audited ``NativeClient`` and its public allauth client in one
transaction; ``revoke-all`` deletes every token of a client (``--disable`` also closes it). Each
write runs in a system context with its own reason, so the audit names the command.
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from skeleton.tenancy.context import system_context


def _problem(exc: ValidationError) -> str:
    if hasattr(exc, "message_dict"):
        return "; ".join(f"{field}: {' '.join(messages)}" for field, messages in exc.message_dict.items())
    return " ".join(exc.messages)


class Command(BaseCommand):
    help = "Register, list or revoke the native clients (desktop apps) of the OIDC provider."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        register = actions.add_parser("register", help="Register a public client with loopback redirects.")
        register.add_argument("--client-id", required=True)
        register.add_argument("--name", required=True)
        register.add_argument(
            "--redirect-uri", action="append", required=True, help="http://127.0.0.1/<path>; any port at runtime."
        )
        register.add_argument("--skip-consent", action="store_true", help="No consent page for this client.")
        register.add_argument("--scope", action="append", default=[], help="Extra core scope, such as files:read.")
        actions.add_parser("list", help="List the registered clients.")
        revoke = actions.add_parser("revoke-all", help="Delete every token of a client.")
        revoke.add_argument("--client-id", required=True)
        revoke.add_argument("--disable", action="store_true", help="Also refuse new sign-ins of the client.")

    def handle(self, *args, action, **options):
        from skeleton.accounts.idp import clients

        try:
            if action == "register":
                with system_context(reason="native-client:register"):
                    record = clients.register_client(
                        client_id=options["client_id"],
                        name=options["name"],
                        redirect_uris=options["redirect_uri"],
                        skip_consent=options["skip_consent"],
                        scopes=options["scope"],
                    )
                self.stdout.write(f"Registered {record.client_id} ({record.sysid}).")
                if not clients.provider_enabled():
                    self.stderr.write("The provider is off until IDP_OIDC_PRIVATE_KEY or its key file is set.")
            elif action == "list":
                with system_context(reason="native-client:list"):
                    rows = clients.clients()
                for row in rows:
                    state = "enabled" if row.enabled else "disabled"
                    consent = "skip-consent" if row.skip_consent else "consent"
                    self.stdout.write(
                        f"{row.client_id}\t{row.name}\t{state}\t{consent}\t{' '.join(row.redirect_uris)}"
                        f"\t{' '.join(row.scopes)}"
                    )
            else:
                with system_context(reason="native-client:revoke"):
                    deleted = clients.revoke_all(client_id=options["client_id"], disable=options["disable"])
                self.stdout.write(f"Revoked {deleted} token(s) of {options['client_id']}.")
        except ValidationError as exc:
            raise CommandError(_problem(exc)) from exc
