"""Sync and list the releases of native clients served by this server (#606).

``sync`` verifies and publishes the new signed releases of one client (``--client-id``) or of
every enabled client of ``SKELETON_NATIVE_CLIENTS_CONFIG``; the same work runs as the periodic
job ``native_clients.sync``. ``list`` shows what each channel serves. Output carries codes and
versions only, never a token or a key.
"""

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from skeleton.tenancy.context import system_context


class Command(BaseCommand):
    help = "Sync (verify and publish) or list the signed releases of the configured native clients."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        sync = actions.add_parser("sync", help="Verify and publish the new releases.")
        sync.add_argument("--client-id", help="Only this client; every enabled client by default.")
        actions.add_parser("list", help="List the published releases.")

    def handle(self, *args, action, **options):
        from skeleton.system.native_clients.config import load_config
        from skeleton.system.native_clients.github import SourceError
        from skeleton.system.native_clients.services import overview
        from skeleton.system.native_clients.sync import sync_client

        try:
            configuration = load_config()
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from exc
        if action == "list":
            with system_context(reason="native-client:list"):
                for client in overview():
                    for release in client["releases"]:
                        state = "withdrawn" if release.withdrawn_at else "published"
                        self.stdout.write(
                            f"{client['client_id']}\t{release.channel}\t{release.os}\t{release.version}\t{state}"
                            f"\t{release.expires_at.isoformat()}"
                        )
            return
        targets = (
            [options["client_id"]]
            if options.get("client_id")
            else sorted(client_id for client_id, entry in configuration.clients.items() if entry.enabled)
        )
        failed = False
        for client_id in targets:
            try:
                with system_context(reason="native-client:sync"):
                    rows = sync_client(client_id)
            except SourceError as exc:
                raise CommandError(f"{client_id}: {exc.code}") from exc
            for row in rows:
                failed = failed or row.outcome != "published"
                detail = row.reason_code or row.version
                self.stdout.write(f"{client_id}\t{row.outcome}\t{row.tag}\t{row.channel}\t{row.os}\t{detail}")
            if not rows:
                self.stdout.write(f"{client_id}\tunchanged")
        if failed:
            raise CommandError("Some releases were refused or the source failed; see the history.")
