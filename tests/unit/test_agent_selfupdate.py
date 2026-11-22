"""The agent's version and self-update from a signed release package (#497).

The command runner is a double: no virtualenv is created and pip never runs.
"""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from packaging.markers import default_environment  # noqa: E402

from deployment_agent import planning, selfupdate  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402
from skeleton.system.packages import TARGET_ENVIRONMENT_KEYS, build_release  # noqa: E402
from tests.unit.agent_helpers import bare_service  # noqa: E402
from tests.unit.release_helpers import PUBLISHER, core_package, lock_for, make_wheel, private_key  # noqa: E402

pytestmark = pytest.mark.posix
HERE = {key: default_environment()[key] for key in TARGET_ENVIRONMENT_KEYS}


class Runner:
    """Records each command; creates what venv and pip would; answers selfcheck with ``version``."""

    def __init__(self, version="0.7.0", fail_on=None):
        self.calls, self.version, self.fail_on = [], version, fail_on

    def __call__(self, arguments, **options):
        self.calls.append(arguments)
        assert options["stdout"] in (subprocess.DEVNULL, subprocess.PIPE) and options["check"] is True
        assert "shell" not in options
        if self.fail_on and self.fail_on in arguments:
            raise subprocess.CalledProcessError(1, arguments, output=b"secret pip output")
        if arguments[1:3] == ["-m", "venv"]:
            Path(arguments[3], "bin").mkdir(parents=True)
        if arguments[-1] == "selfcheck":
            return SimpleNamespace(stdout=json.dumps({"ok": True, "version": self.version}).encode())
        return SimpleNamespace(stdout=b"")


def release(tmp_path, version="0.7.0", *, environment=None, agent=True, name=None):
    private = tmp_path / "publisher.pem"
    folder = tmp_path / f"build-{version}-{name or 'r'}"
    folder.mkdir()
    core = core_package(folder, private, version)
    wheels = [make_wheel(folder / "w", "asgiref", "3.12.1")]
    target = {
        "id": "here",
        "environment": environment or HERE,
        "lock": lock_for(folder / "lock.txt", wheels),
        "wheels": wheels,
    }
    agent_wheel = make_wheel(folder / "agent", "skeleton-deployment-agent", version) if agent else None
    return build_release(core, folder / "release.skapp", private, targets=[target], agent_wheel=agent_wheel)


@pytest.fixture
def agent(tmp_path, monkeypatch):
    service = bare_service(tmp_path)
    _, public = private_key(tmp_path / "publisher.pem")
    root = tmp_path / "agent-root"
    root.mkdir()
    service.config.update(
        approved_publishers={PUBLISHER: public}, agent_root=str(root), config_path=str(tmp_path / "agent.json")
    )
    state = service.state()
    state["inventory"]["core"]["publisher"] = PUBLISHER
    selfupdate.atomic_json(service.store.root / "active.json", state)
    monkeypatch.setattr(selfupdate, "agent_version", lambda: "0.6.0")
    return SimpleNamespace(service=service, root=root, tmp=tmp_path)


def uploaded(agent, path):
    return agent.service.upload(Path(path).read_bytes())["digest"]


def update(agent, digest, runner=None):
    return selfupdate.update(agent.service, digest, actor_id=None, run=runner or Runner())


def test_successful_update_switches_the_link_and_asks_for_a_restart(agent):
    digest = uploaded(agent, release(agent.tmp))
    runner = Runner()
    assert update(agent, digest, runner) == {"state": "restarting", "version": "0.7.0"}
    name = f"0.7.0-{digest[:12]}"
    assert os.readlink(agent.root / "current") == f"versions/{name}"
    assert (agent.root / "versions" / name / "READY").is_file()
    state = json.loads((agent.root / "state.json").read_text())
    assert (state["current"], state["previous"], state["version"], state["release"]) == (name, None, "0.7.0", digest)
    install = next(call for call in runner.calls if "install" in call)
    assert "--no-index" in install and "--require-hashes" in install and "--only-binary=:all:" in install
    lock = (agent.root / "versions" / name / "requirements.lock").read_text()
    assert "django-skeleton==0.7.0 --hash=sha256:" in lock and "skeleton-deployment-agent==0.7.0 --hash=sha256:" in lock
    assert sorted(p.name for p in (agent.root / "versions" / name / "wheels").iterdir()) == [
        "asgiref-3.12.1-py3-none-any.whl",
        "django_skeleton-0.7.0-py3-none-any.whl",
        "skeleton_deployment_agent-0.7.0-py3-none-any.whl",
    ]
    (event,) = (agent.service.store.root / "agent-updates").iterdir()
    assert json.loads(event.read_text())["to"] == "0.7.0"


def test_every_refusal_leaves_the_current_agent(agent, tmp_path, monkeypatch):
    del agent.service.config["agent_root"]
    with pytest.raises(AgentError, match="agent_self_update_unavailable"):
        update(agent, "0" * 64)
    agent.service.config["agent_root"] = str(agent.root)
    Path(agent.service.config["maintenance_marker"]).write_text("{}")
    with pytest.raises(AgentError, match="recovery_or_operation_pending"):
        update(agent, "0" * 64)
    Path(agent.service.config["maintenance_marker"]).unlink()
    with pytest.raises(AgentError, match="package_verification_failed"):
        update(agent, "0" * 64)
    with pytest.raises(AgentError, match="release_without_agent"):
        update(agent, uploaded(agent, release(tmp_path, agent=False, name="noagent")))
    with pytest.raises(AgentError, match="agent_target_unavailable"):
        update(
            agent, uploaded(agent, release(tmp_path, environment={**HERE, "platform_machine": "riscv-test"}, name="t"))
        )
    digest = uploaded(agent, release(tmp_path, name="ok"))
    monkeypatch.setattr(selfupdate, "agent_version", lambda: "0.7.0")
    with pytest.raises(AgentError, match="agent_already_current"):
        update(agent, digest)
    monkeypatch.setattr(selfupdate, "agent_version", lambda: "0.8.0")
    with pytest.raises(AgentError, match="agent_downgrade_forbidden"):
        update(agent, digest)
    assert not (agent.root / "current").exists() and not (agent.root / "state.json").exists()


def test_publisher_must_be_the_core_publisher_or_listed(agent):
    digest = uploaded(agent, release(agent.tmp))
    state = agent.service.state()
    state["inventory"]["core"]["publisher"] = "another-publisher"
    selfupdate.atomic_json(agent.service.store.root / "active.json", state)
    with pytest.raises(AgentError) as raised:
        update(agent, digest)
    assert (raised.value.code, raised.value.status) == ("agent_publisher_not_allowed", 403)
    agent.service.config["agent_publishers"] = [PUBLISHER]
    assert update(agent, digest)["state"] == "restarting"


@pytest.mark.parametrize("runner", [Runner(fail_on="check"), Runner(version="0.6.9")])
def test_failed_build_or_selfcheck_keeps_current_and_state(agent, runner):
    first = uploaded(agent, release(agent.tmp, "0.6.5", name="first"))
    update(agent, first, Runner(version="0.6.5"))
    before = (os.readlink(agent.root / "current"), (agent.root / "state.json").read_bytes())
    digest = uploaded(agent, release(agent.tmp, name="second"))
    with pytest.raises(AgentError) as raised:
        update(agent, digest, runner)
    assert raised.value.code == "agent_build_failed" and "secret" not in str(raised.value)
    assert (os.readlink(agent.root / "current"), (agent.root / "state.json").read_bytes()) == before
    assert not (agent.root / "versions" / f"0.7.0-{digest[:12]}").exists()


def test_switch_keeps_only_current_and_previous_and_rollback_returns(agent, monkeypatch):
    for version in ("0.6.5", "0.6.6", "0.7.0"):
        digest = uploaded(agent, release(agent.tmp, version, name=version))
        update(agent, digest, Runner(version=version))
    versions = sorted(path.name.split("-")[0] for path in (agent.root / "versions").iterdir())
    assert versions == ["0.6.6", "0.7.0"]
    restored = selfupdate.rollback(agent.service.config)
    assert restored["version"] == "0.6.6" and os.readlink(agent.root / "current").startswith("versions/0.6.6-")
    state = json.loads((agent.root / "state.json").read_text())
    assert state["current"].startswith("0.6.6-") and state["previous"].startswith("0.7.0-")
    (agent.root / "state.json").write_text(json.dumps({**state, "previous": None}))
    with pytest.raises(AgentError, match="agent_rollback_unavailable"):
        selfupdate.rollback(agent.service.config)


def test_agent_install_from_file_and_unapproved_publisher(agent, tmp_path):
    path = release(tmp_path, name="file")
    config = {**agent.service.config, "storage_root": str(agent.service.store.root)}
    result = selfupdate.install_from_file(config, path, run=Runner())
    assert result["current"].startswith("0.7.0-") and "systemctl restart skeleton-agent" in result["next_steps"]
    other = tmp_path / "other"
    other.mkdir()
    private_key(other / "publisher.pem")
    foreign = release(other, "0.7.1", name="foreign")
    with pytest.raises(AgentError, match="package_verification_failed"):
        selfupdate.install_from_file(config, foreign, run=Runner(version="0.7.1"))


def test_core_update_needs_an_agent_on_the_target_line(monkeypatch):
    core = [{"kind": "core", "version": "0.7.0"}]
    monkeypatch.setattr(selfupdate, "agent_version", lambda: "0.6.4")
    with pytest.raises(AgentError) as raised:
        planning.check_agent_line(core)
    assert (raised.value.code, raised.value.status) == ("agent_update_required", 409)
    assert raised.value.details == [
        {
            "code": "agent_update_required",
            "component": "skeleton-deployment-agent",
            "required": ">=0.7.0",
            "found": "0.6.4",
        }
    ]
    for version in ("0.7.0", "0.7.3", "0.8.0"):
        monkeypatch.setattr(selfupdate, "agent_version", lambda version=version: version)
        planning.check_agent_line(core)
    monkeypatch.setattr(selfupdate, "agent_version", lambda: None)
    planning.check_agent_line(core)
    monkeypatch.setattr(selfupdate, "agent_version", lambda: "0.6.4")
    planning.check_agent_line([{"kind": "app", "version": "9.0.0"}])


def test_inventory_and_upload_report_the_agent(agent):
    inventory = agent.service.inventory()
    assert inventory["agent"] == {"version": "0.6.0", "self_update": True}
    result = agent.service.upload(release(agent.tmp, name="offer").read_bytes())
    assert result["agent"] == {"version": "0.7.0", "update_required": True}


def test_selfcheck_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate, "agent_version", lambda: "0.7.0")
    (tmp_path / "state").mkdir()
    before = sorted(tmp_path.rglob("*"))
    assert selfupdate.selfcheck({"storage_root": str(tmp_path / "state")}) == {"ok": True, "version": "0.7.0"}
    assert sorted(tmp_path.rglob("*")) == before
    with pytest.raises(AgentError):
        selfupdate.selfcheck({"storage_root": str(tmp_path / "missing")})


class ClusterNode:
    """The coordination of a cluster (#689), in memory: the target and the other nodes' versions."""

    def __init__(self, root, versions=("0.6.0", "0.6.0", "0.7.0")):
        self.settings = SimpleNamespace(node_id="node-a")
        self.is_primary, self.problem, self.target, self.root = True, None, None, root
        self.versions, self.seen = list(versions), []

    def set_agent_target(self, version, release):
        self.target = {"version": version, "release": release, "epoch": 1}

    def agent_target(self):
        return self.target

    def nodes(self):
        # Whether this node had already switched when the other one was still behind.
        version = self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]
        self.seen.append((version, (self.root / "current").exists()))
        return [
            {"node_id": "node-a", "alive": True, "agent_version": "0.6.0"},
            {"node_id": "node-b", "alive": True, "agent_version": version},
            {"node_id": "node-c", "alive": False, "agent_version": "0.5.0"},  # Gone: not waited for.
        ]


class ArtifactChannel:
    def __init__(self, path):
        self.path, self.calls = path, []

    def fetch_artifact(self, digest):
        self.calls.append(digest)
        return self.path


def test_in_a_cluster_the_primary_sets_the_target_and_switches_after_the_others(agent, monkeypatch):
    import threading

    monkeypatch.setattr(selfupdate, "CLUSTER_POLL_S", 0.01)
    restarts = []
    agent.service.coordinator = ClusterNode(agent.root)
    agent.service.request_restart = lambda: restarts.append(True)
    digest = uploaded(agent, release(agent.tmp))
    assert update(agent, digest) == {"state": "rolling", "version": "0.7.0"}
    assert agent.service.coordinator.target == {"version": "0.7.0", "release": digest, "epoch": 1}
    worker = next(thread for thread in threading.enumerate() if thread.name == "deployment-agent-update")
    worker.join(10)
    assert agent.service.coordinator.seen == [("0.6.0", False), ("0.6.0", False), ("0.7.0", False)]
    assert os.readlink(agent.root / "current") == f"versions/0.7.0-{digest[:12]}" and restarts == [True]


def test_a_cluster_update_is_refused_like_on_one_node_before_any_target(agent):
    agent.service.coordinator = ClusterNode(agent.root)
    agent.service.coordinator.is_primary = False
    with pytest.raises(AgentError, match="not_primary"):
        update(agent, "0" * 64)
    agent.service.coordinator.is_primary = True
    with pytest.raises(AgentError, match="package_verification_failed"):
        update(agent, "0" * 64)
    assert agent.service.coordinator.target is None


def follow(agent, path, target_digest, version="0.7.0"):
    agent.service.coordinator = ClusterNode(agent.root)
    agent.service.coordinator.target = {"version": version, "release": target_digest, "epoch": 1}
    agent.service.channel = ArtifactChannel(path)
    restarts = []
    agent.service.request_restart = lambda: restarts.append(True)
    return restarts


def test_a_node_follows_the_target_over_the_channel_and_restarts(agent):
    path = release(agent.tmp)
    digest = uploaded(agent, path)
    restarts = follow(agent, path, digest)
    assert selfupdate.follow_agent_target(agent.service, run=Runner()) is True
    assert agent.service.channel.calls == [digest] and restarts == [True]
    assert os.readlink(agent.root / "current") == f"versions/0.7.0-{digest[:12]}"
    (event,) = (agent.service.store.root / "agent-updates").iterdir()
    assert json.loads(event.read_text())["to"] == "0.7.0"


def test_a_node_refuses_a_tampered_or_unexpected_package_or_an_unapproved_publisher(agent, tmp_path):
    path = release(agent.tmp)
    digest = uploaded(agent, path)
    tampered = tmp_path / "tampered.skapp"
    tampered.write_bytes(path.read_bytes()[:-20] + b"\0" * 20)
    follow(agent, tampered, digest)
    with pytest.raises(AgentError, match="package_verification_failed"):
        selfupdate.follow_agent_target(agent.service, run=Runner())
    other = release(tmp_path, "0.7.1", name="other")  # Valid, but not the package the primary set.
    follow(agent, other, digest)
    with pytest.raises(AgentError, match="package_verification_failed"):
        selfupdate.follow_agent_target(agent.service, run=Runner())
    state = agent.service.state()
    state["inventory"]["core"]["publisher"] = "another-publisher"
    selfupdate.atomic_json(agent.service.store.root / "active.json", state)
    follow(agent, path, digest)
    with pytest.raises(AgentError, match="agent_publisher_not_allowed"):
        selfupdate.follow_agent_target(agent.service, run=Runner())
    assert not (agent.root / "current").exists()


def test_without_agent_root_or_with_the_target_installed_a_node_changes_nothing(agent, monkeypatch):
    path = release(agent.tmp)
    digest = uploaded(agent, path)
    restarts = follow(agent, path, digest)
    del agent.service.config["agent_root"]  # Containers: the agent comes from the image.
    assert selfupdate.follow_agent_target(agent.service, run=Runner()) is False
    assert agent.service.coordinator.problem == "agent_update_manual" and agent.service.channel.calls == []
    agent.service.config["agent_root"] = str(agent.root)
    monkeypatch.setattr(selfupdate, "agent_version", lambda: "0.7.0")
    restarts = follow(agent, path, digest)
    assert selfupdate.follow_agent_target(agent.service, run=Runner()) is False
    assert agent.service.channel.calls == [] and restarts == []
