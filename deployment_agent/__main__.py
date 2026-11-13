"""Standalone CLI stays usable when the managed Django release cannot start."""

import argparse
import json
import sys
import time

from .config import Config
from .server import StatusTickets, serve
from .service import DeploymentService


def build_parser():
    parser = argparse.ArgumentParser(prog="skeleton-deployment-agent")
    parser.add_argument("--config", required=True, help="Private operator-owned JSON configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    initialize = commands.add_parser(
        "initialize", help="Adopt an explicitly inventoried signed base, refusing modified/unknown forks"
    )
    initialize.add_argument("--inventory", required=True)
    initialize.add_argument("--core-package", required=True)
    initialize.add_argument("--app-package", action="append", default=[])
    commands.add_parser("inventory")
    commands.add_parser("journal")
    status = commands.add_parser("operation")
    status.add_argument("operation_id")
    upload = commands.add_parser("upload")
    upload.add_argument("artifact")
    preview = commands.add_parser("preview")
    preview.add_argument("action", choices=("install", "update", "uninstall", "reinstall", "core_update"))
    preview.add_argument("--artifact", action="append", default=[], help="Previously uploaded artifact digest")
    preview.add_argument("--app-id", action="append", default=[])
    submit = commands.add_parser("submit")
    submit.add_argument("--plan-id", required=True)
    submit.add_argument("--plan-digest", required=True)
    submit.add_argument("--generation", required=True, type=int)
    submit.add_argument("--operation-id", required=True)
    submit.add_argument("--actor-id", required=True)
    submit.add_argument("--request-id", required=True)
    commands.add_parser("check-updates")
    fetch = commands.add_parser("fetch-release")
    fetch.add_argument("component_id")
    fetch.add_argument("version")
    fetch.add_argument("source_id")
    recovery = commands.add_parser("recover", help="Offline exclusive recovery; stop the agent daemon first")
    recovery.add_argument("operation_id")
    recovery.add_argument("action", choices=("restore", "complete", "abort"))
    recovery.add_argument(
        "--acknowledge-new-writes",
        action="store_true",
        help="Operator reviewed loss/reconciliation of writes after cutover",
    )
    commands.add_parser("revoke-status-tickets")
    prune = commands.add_parser(
        "prune", help="Offline: list, and with --apply remove, releases, plans, images and backups no operation needs"
    )
    prune.add_argument("--plan-days", type=int, default=7, help="Unsubmitted previews older than N days")
    prune.add_argument("--backup-days", type=int, default=None, help="Also backups and logs older than N days")
    prune.add_argument("--apply", action="store_true", help="Remove; without it, only list")
    return parser


def revoke_status_tickets(service):
    StatusTickets(service).revoke()
    return {"state": "revoked"}


# Offline commands act on the journal directly; the agent daemon must be stopped for initialize
# and recover. Each returns the JSON printed on success.
OFFLINE = {
    "initialize": lambda service, options: service.initialize(
        options.inventory, core_package=options.core_package, app_packages=options.app_package
    ),
    "recover": lambda service, options: service.recover(
        options.operation_id, options.action, acknowledge_new_writes=options.acknowledge_new_writes
    ),
    "journal": lambda service, options: {
        "operations": [service.public_operation(row) for row in service.store.operations()]
    },
    "prune": lambda service, options: service.prune(
        plan_days=options.plan_days, backup_days=options.backup_days, apply=options.apply
    ),
    "revoke-status-tickets": lambda service, options: revoke_status_tickets(service),
}
ONLINE = {
    "inventory": lambda client, options: client.inventory(),
    "operation": lambda client, options: client.operation(options.operation_id),
    "upload": lambda client, options: client.upload(options.artifact),
    "submit": lambda client, options: client.submit(
        options.plan_id,
        options.plan_digest,
        options.generation,
        options.operation_id,
        options.actor_id,
        options.request_id,
    ),
    "check-updates": lambda client, options: client.check_updates(),
    "fetch-release": lambda client, options: client.fetch_release(
        options.component_id, options.version, options.source_id
    ),
}


def follow_preview(client, options):
    """The plan is prepared in the agent (#298): follow it until it is ready (plan) or failed (None)."""
    result = client.preview(options.action, artifacts=options.artifact, app_ids=options.app_id)
    while result.get("state") == "preparing":
        time.sleep(2)
        result = client.plan(result["id"])
    if result.get("state") == "failed":
        failure = {"error": result.get("error"), "details": result.get("details")}
        print(json.dumps(failure), file=sys.stderr)
        return None
    return result.get("plan", result)


def main():
    options = build_parser().parse_args()
    try:
        config = Config.load(options.config)
        service = DeploymentService(config)
        if options.command == "serve":
            serve(service)
            return 0
        if options.command in OFFLINE:
            result = OFFLINE[options.command](service, options)
        else:
            # Online commands go through the same bounded authenticated interface as the platform admin.
            from skeleton.system.deployment_client import AgentClient

            client = AgentClient(
                config["socket_path"], config["token_file"], timeout=config.get("command_timeout", 1800)
            )
            if options.command == "preview":
                result = follow_preview(client, options)
                if result is None:
                    return 1
            else:
                result = ONLINE[options.command](client, options)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ValueError as error:
        print(json.dumps({"error": getattr(error, "code", type(error).__name__)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
