"""Deploy artifacts apply the hardening the project expects (#318); no Docker, systemd or database."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APPLICATION_SERVICES = ("web", "worker", "scheduler", "migrator")
UNITS = ROOT / "deploy" / "systemd"


def compose_blocks(name="compose.yaml"):
    """Top-level blocks and service blocks of a compose file, read as text (PyYAML is not in the locks)."""
    text = (ROOT / name).read_text(encoding="utf-8")
    top = dict(re.findall(r"^(\S[^:\n]*):[^\n]*\n((?:[ #][^\n]*\n|\n)*)", text, re.MULTILINE))
    services = dict(re.findall(r"^  (\w[\w-]*):\n((?:    [^\n]*\n|\s*#[^\n]*\n)*)", top["services"], re.MULTILINE))
    return top, services


# The Coolify sandbox (#369) runs the same application services, plus its one-shot migrate.
COMPOSE_FILES = [
    ("compose.yaml", APPLICATION_SERVICES),
    ("compose.sandbox.yaml", ("migrate", "web", "worker", "scheduler")),
]


@pytest.mark.parametrize(("name", "application"), COMPOSE_FILES)
def test_compose_application_services_are_hardened(name, application):
    top, services = compose_blocks(name)
    anchor = top["x-hardened"]
    assert "  read_only: true\n" in anchor
    assert re.search(r"  tmpfs:\n    - /tmp:size=\d+m,mode=1777\n", anchor)
    assert '  cap_drop: ["ALL"]\n' in anchor
    assert '  security_opt: ["no-new-privileges:true"]\n' in anchor
    for service in application:
        assert services[service].startswith("    <<: *hardened\n"), (name, service)
        for key in ("read_only", "cap_drop", "security_opt", "tmpfs"):
            assert f"    {key}:" not in services[service], (name, service, key)  # No override of the anchor.
    assert set(application) < set(services) and "<<: *hardened" not in services["postgres"]


@pytest.mark.parametrize("name", ["compose.yaml", "compose.sandbox.yaml"])
def test_redis_password_is_not_in_rendered_command(name):
    _, services = compose_blocks(name)
    redis = services["redis"]
    command = next(line for line in redis.splitlines() if line.startswith("    command:"))
    assert "${REDIS_PASSWORD" not in command
    assert '\\"$$REDIS_PASSWORD\\"' in command  # Expanded by the container shell, not by Compose.
    assert "exec docker-entrypoint.sh redis-server" in command  # The image still drops to the redis user.
    assert "REDIS_PASSWORD: ${REDIS_PASSWORD:?" in redis


def test_examples_are_optional_in_the_image():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG INCLUDE_EXAMPLES=true" in dockerfile
    assert re.search(r'if \[ "\$INCLUDE_EXAMPLES" != "true" \]; then rm -rf \./examples', dockerfile)


def unit(name):
    return (UNITS / name).read_text(encoding="utf-8").splitlines()


def test_systemd_units_exist_and_restrict_privileges():
    names = ("skeleton-web.service", "skeleton-worker.service", "skeleton-scheduler.service", "skeleton-agent.service")
    assert sorted(path.name for path in UNITS.glob("*.service")) == sorted(names)
    for name in names[:3]:
        lines = unit(name)
        for directive in (
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "PrivateTmp=yes",
            "PrivateDevices=yes",
            "ProtectKernelTunables=yes",
            "ProtectControlGroups=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        ):
            assert directive in lines, (name, directive)
        writable = next(line for line in lines if line.startswith("ReadWritePaths="))
        assert all(path.startswith("-/") for path in writable.removeprefix("ReadWritePaths=").split()), writable
    agent = unit("skeleton-agent.service")
    assert "ProtectHome=yes" in agent and "PrivateTmp=yes" in agent
    for directive in ("NoNewPrivileges=", "ProtectSystem=", "PrivateDevices=", "ProtectKernelTunables="):
        assert not any(line.startswith(directive) for line in agent), directive
    assert "sudo does not work under NoNewPrivileges=yes" in "\n".join(agent)  # The exception is explained.
    assert {"KillSignal=SIGTERM", "KillMode=mixed", "TimeoutStopSec=1800"} <= set(agent)  # Kept from #280.


def test_deployment_doc_references_unit_files():
    doc = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "skeleton-web.service" in doc and "skeleton-agent.service" in doc and "deploy/systemd/" in doc
    assert "# skeleton-web.service" not in doc and "# skeleton-agent.service" not in doc


def test_bootstrap_bounds_only_the_runtime_role():
    sql = (ROOT / "deploy" / "postgres" / "bootstrap.sql").read_text(encoding="utf-8")
    assert "ALTER ROLE skeleton_runtime SET lock_timeout = '10s';" in sql
    assert "ALTER ROLE skeleton_runtime SET idle_in_transaction_session_timeout = '60s';" in sql
    assert "statement_timeout" not in sql
    assert not re.search(r"ALTER ROLE skeleton_owner SET (lock|idle)", sql)
