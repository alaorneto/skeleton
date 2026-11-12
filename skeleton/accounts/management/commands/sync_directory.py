"""Sync LDAP providers with their directories (#33); schedule it with cron or a systemd timer."""

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Deactivate memberships of people who left the directory of an LDAP provider (one transaction per "
        "organization, never reactivating anyone). --provision also admits people who logged in through the "
        "provider but never had a membership, following the organization's rules. Prints counts and UUIDs only."
    )

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--provider", help="provider_id of one LDAP provider.")
        target.add_argument("--all", action="store_true", help="Every enabled LDAP provider.")
        parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
        parser.add_argument("--provision", action="store_true", help="Also admit directory users (off by default).")
        parser.add_argument("--max-deactivations", type=int, default=None, help="Abort an organization above this.")
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options):
        from skeleton.accounts.ldap.sync import providers, sync_provider

        rows = providers(options["provider"])
        if options["provider"] and not rows:
            raise CommandError("No enabled LDAP provider with this provider_id.")
        if options["max_deactivations"] is not None and options["max_deactivations"] < 0:
            raise CommandError("--max-deactivations must be zero or more.")
        reports = [
            sync_provider(
                row,
                dry_run=options["dry_run"],
                provision=options["provision"],
                max_deactivations=options["max_deactivations"],
            )
            for row in rows
        ]
        if options["format"] == "json":
            self.stdout.write(json.dumps([report.as_dict() for report in reports], sort_keys=True))
        else:
            for report in reports:
                status = report.aborted or ("dry-run" if report.dry_run else "ok")
                self.stdout.write(
                    f"{report.provider_id} organization={report.organization_id} status={status} "
                    f"directory={report.directory_users} deactivated={len(report.deactivated)} "
                    f"skipped_last_admin={len(report.skipped_last_admin)} provisioned={len(report.provisioned)} "
                    f"requested={len(report.requested)}"
                )
        failed = [report.provider_id for report in reports if report.aborted]
        if failed:
            raise CommandError("Directory sync aborted for: " + ", ".join(failed))
