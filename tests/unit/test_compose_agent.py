"""Both Compose files run the deployment agent (#654): read as text, without Docker.

The rendered configuration is checked again by the CI (``docker compose config``) and the stack
itself by the "Stack Compose com o agente" job.
"""

import re

import pytest

from tests.unit.test_deploy_hardening import ROOT, compose_blocks

FILES = ("compose.yaml", "compose.sandbox.yaml")
APPLICATION = ("web", "worker", "scheduler")
LAUNCHER = (
    '["python", "-m", "deployment_agent.launcher", "--service", "{service}", "--control", "/control/services", '
    '"--state", "/control-state", "--releases-root", "/releases", "--active", "/releases/current"'
)


@pytest.mark.parametrize("name", FILES)
def test_the_agent_is_the_only_service_with_the_owner_and_nobody_mounts_the_docker_socket(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    _, services = compose_blocks(name)
    assert "docker.sock" not in text and "/var/run/docker" not in text
    assert not {"migrate", "migrator"} & set(services)  # The agent migrates (#652).
    owners = [service for service, block in services.items() if "skeleton_owner:" in block]
    assert owners == ["agent"]
    assert "SKELETON_MIGRATION_DATABASE_URL: postgres://skeleton_owner:" in services["agent"]
    for service in APPLICATION:
        assert "MIGRATION_DATABASE_URL" not in services[service], service
        assert "OWNER_DB_PASSWORD" not in services[service], service


@pytest.mark.parametrize("name", FILES)
def test_the_application_services_run_the_active_release_through_the_launcher(name):
    top, services = compose_blocks(name)
    for service in APPLICATION:
        command = next(line for line in services[service].splitlines() if line.startswith("    command:"))
        assert LAUNCHER.format(service=service) in command, (name, service)
        assert "    volumes: *app-volumes\n" in services[service], (name, service)
        assert "build: ." in services[service]  # The runtime image, which carries the launcher.
    volumes = top["x-app-volumes"]
    assert "  - releases:/releases:ro\n" in volumes and "  - control:/control:ro\n" in volumes
    assert "  - control_state:/control-state\n" in volumes and "  - files:/files\n" in volumes
    environment = top["x-app-environment"]
    for line in (
        "  SKELETON_AGENT_SOCKET: /control/agent.sock\n",
        "  SKELETON_AGENT_TOKEN_FILE: /control/agent.token\n",
        "  SKELETON_MAINTENANCE_MARKER: /control/maintenance\n",
        "  SKELETON_SETUP_SCHEMA: agent\n",
        "  SKELETON_FILES_ROOT: /files\n",
    ):
        assert line in environment, (name, line)


@pytest.mark.parametrize("name", FILES)
def test_the_agent_writes_releases_and_control_and_only_reads_the_launcher_state(name):
    _, services = compose_blocks(name)
    agent = services["agent"]
    assert re.search(r"\n      target: agent\n", agent)
    for mount in ("agent_private:/agent/private", "agent_state:/agent/state", "releases:/releases", "control:/control"):
        assert f"      - {mount}\n" in agent, mount
    assert "      - control_state:/control-state:ro\n" in agent
    assert "agent_private" not in "".join(services[service] for service in APPLICATION)
    assert (
        "SKELETON_AGENT_PUBLISHER_KEY: ${SKELETON_AGENT_PUBLISHER_KEY:-/opt/agent/publishers/skeleton.pub.pem}" in agent
    )
    assert "SKELETON_AGENT_SOURCE: ${SKELETON_AGENT_SOURCE:-alaorneto/skeleton}" in agent


def test_everything_is_on_in_both_files():
    sandbox, _ = compose_blocks("compose.sandbox.yaml")
    assert "  SKELETON_BUNDLED_APPS: calendar,ledger\n" in sandbox["x-features"]
    assert '  SKELETON_ENABLE_EXAMPLE_APP: "true"\n' in sandbox["x-features"]
    assert '  SKELETON_BILLING_ENABLED: "true"\n' in sandbox["x-features"]
    local, _ = compose_blocks("compose.yaml")
    assert "  SKELETON_BUNDLED_APPS: ${SKELETON_BUNDLED_APPS-calendar,ledger}\n" in local["x-features"]
    assert "  SKELETON_ENABLE_EXAMPLE_APP: ${SKELETON_ENABLE_EXAMPLE_APP:-true}\n" in local["x-features"]
    assert "  SKELETON_BILLING_ENABLED: ${SKELETON_BILLING_ENABLED:-true}\n" in local["x-features"]
    for name in ("compose.yaml", "compose.sandbox.yaml"):
        top, services = compose_blocks(name)
        assert "  <<: *features\n" in top["x-app-environment"]
        assert "      <<: *features\n" in services["agent"]


def test_the_images_create_the_volume_mount_points_for_the_service_user():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile.split(" AS runtime\n", 1)[1]
    assert "mkdir -p /files /releases /control /control-state && chown skeleton:skeleton" in runtime
    agent = dockerfile.split(" AS agent\n", 1)[1].split(" AS runtime\n", 1)[0]
    assert "pip install --no-cache-dir --no-deps /core-wheel/*.whl /agent-wheel/*.whl" in agent
    assert "PYTHONPATH" not in agent  # The agent's hooks run without it: the package is installed.
