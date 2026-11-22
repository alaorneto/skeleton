"""The phases of an update across the nodes of a cluster (#681), against PostgreSQL.

The primary's ``Rollout`` and a follower's ``Follower`` share the coordination tables of the test
database; the services, backends and the channel are fakes that record what each node did.
"""

import os
import tarfile
import threading
from pathlib import Path

import pytest

from deployment_agent import execution, rollout
from deployment_agent.coordination import ClusterSettings, Coordinator
from deployment_agent.releases import owned_snapshot
from deployment_agent.storage import AgentError, atomic_json, digest

OWNER = os.environ.get("TEST_MIGRATION_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not OWNER, reason="TEST_MIGRATION_DATABASE_URL is required.")


def coordinator(node_id, tmp_path):
    settings = ClusterSettings(
        node_id=node_id,
        role="candidate",
        cluster_url=f"https://{node_id}.cluster.test:9443",
        preferred=True,
        database_url=OWNER,
    )
    return Coordinator(settings, storage_root=tmp_path / "shared")


def make_release(root, name, files):
    release = root / name
    release.mkdir(parents=True)
    for path, text in files.items():
        (release / path).parent.mkdir(parents=True, exist_ok=True)
        (release / path).write_text(text, encoding="utf-8")
    atomic_json(release / "inventory.json", {"generation": 2, "core": {}, "apps": []}, 0o644)
    owned = owned_snapshot(release)
    atomic_json(release / "owned-files.json", owned, 0o644)
    return release, digest(owned)


class Backend:
    def __init__(self, log):
        self.log = log

    def drain(self, release, operation):
        self.log.append(("drain", Path(release).name))

    def cutover(self, release):
        self.log.append(("cutover", Path(release).name))

    def start(self, release, operation):
        self.log.append(("start", Path(release).name))


class Channel:
    def __init__(self, source_root, tmp_path):
        self.source_root = source_root
        self.tmp_path = tmp_path

    def fetch_release(self, name):
        archive = self.tmp_path / f"{name}-{os.urandom(4).hex()}.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(self.source_root / name, arcname=name)
        return archive


class FollowerService:
    def __init__(self, tmp_path, node, primary_releases, active=None):
        self.log = []
        self.coordinator = node
        self.backend = Backend(self.log)
        self.config = {
            "maintenance_marker": str(tmp_path / f"{node.settings.node_id}.maintenance"),
            "releases_root": str(tmp_path / f"{node.settings.node_id}-releases"),
            "health_urls": ["http://127.0.0.1:1/health/live/"],
        }
        Path(self.config["releases_root"]).mkdir()
        self.channel = Channel(primary_releases, tmp_path)
        self.active = active

    def state(self):
        if self.active is None:
            raise AgentError("deployment_not_initialized", 409)
        return {"release": str(self.active), "inventory": {"generation": 1}, "owned_files": {}}

    def _write_active(self, release, inventory, owned):
        self.active = Path(release)
        self.log.append(("active", Path(release).name))


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    monkeypatch.setattr(rollout, "POLL_S", 0.05)
    monkeypatch.setattr(execution, "health", lambda service, inventory: service.log.append(("health", "ok")))
    releases = tmp_path / "primary-releases"
    new, release_digest = make_release(releases, "2-plan", {"manage.py": "print('new')\n"})
    old_dir = tmp_path / "node-b-old"
    old_dir.mkdir()
    primary, follower_node = coordinator("node-a", tmp_path), coordinator("node-b", tmp_path)
    try:
        primary.ensure_schema()
        for table in ('"primary"', "node", "rollout"):
            primary._execute(f"DELETE FROM skeleton_deploy.{table}")
        assert primary.try_acquire()
        primary.heartbeat()
        follower_node.heartbeat()
        yield primary, FollowerService(tmp_path, follower_node, releases, active=old_dir), new, release_digest
    finally:  # The lock is released even when the setup fails: the next test needs it.
        primary.close()
        follower_node.close()


class PrimaryService:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.rollout = None


def plan_record(release, release_digest):
    return {
        "plan": {"release": str(release), "release_digest": release_digest, "generation": 1},
        "inventory": {"generation": 2},
    }


def follow_until(service, stop):
    follower = rollout.Follower(service)
    follower.started_services = True  # Already running: only the phases of the operation act here.
    while not stop.is_set():
        follower.tick()
        stop.wait(0.05)


def test_the_phases_of_a_window_update_reach_the_follower_in_order(cluster):
    primary, service, new, release_digest = cluster
    stop = threading.Event()
    thread = threading.Thread(target=follow_until, args=(service, stop), daemon=True)
    thread.start()
    try:
        run = rollout.begin(PrimaryService(primary), {"id": "op-1"}, plan_record(new, release_digest))
        assert run.nodes == ["node-b"]
        run.wait("prepare", timeout=10, code="follower_prepare_failed")
        assert (Path(service.config["releases_root"]) / "2-plan" / "manage.py").exists()
        run.wait("drain", timeout=10, code="follower_drain_failed")
        assert Path(service.config["maintenance_marker"]).exists() and ("drain", "node-b-old") in service.log
        run.wait("cutover", timeout=10, code="follower_cutover_failed")
        assert service.active.name == "2-plan"
        assert [entry for entry in service.log if entry[0] in ("cutover", "start", "health")] == [
            ("cutover", "2-plan"),
            ("start", "2-plan"),
            ("health", "ok"),
        ]
        run.wait("resume", timeout=10, code="follower_resume_failed")
        assert not Path(service.config["maintenance_marker"]).exists()
        run.phase("done")
    finally:
        stop.set()
        thread.join(5)


def test_a_follower_that_cannot_install_fails_the_prepare_with_its_reason(cluster, monkeypatch):
    primary, service, new, _ = cluster
    stop = threading.Event()
    thread = threading.Thread(target=follow_until, args=(service, stop), daemon=True)
    thread.start()
    try:
        run = rollout.begin(PrimaryService(primary), {"id": "op-2"}, plan_record(new, "0" * 64))
        with pytest.raises(AgentError) as refused:
            run.wait("prepare", timeout=10, code="follower_prepare_failed")
        assert refused.value.code == "follower_prepare_failed"
        assert refused.value.details == [{"code": "follower_release_mismatch", "component": "node-b"}]
        assert not (Path(service.config["releases_root"]) / "2-plan").exists()  # Nothing kept.
    finally:
        stop.set()
        thread.join(5)


def test_a_silent_node_times_out_and_the_abort_brings_it_back(cluster):
    primary, service, new, release_digest = cluster
    run = rollout.begin(PrimaryService(primary), {"id": "op-3"}, plan_record(new, release_digest))
    with pytest.raises(AgentError) as silent:
        run.wait("drain", timeout=0.2, code="follower_drain_failed")
    assert silent.value.details == [{"code": "node_timeout", "component": "node-b"}]
    marker = Path(service.config["maintenance_marker"])
    marker.write_text("{}")
    run.phase("abort")
    rollout.Follower(service).tick()
    assert not marker.exists() and ("start", "node-b-old") in service.log


def test_an_order_of_an_older_epoch_is_ignored(cluster, tmp_path):
    primary, service, new, release_digest = cluster
    run = rollout.begin(PrimaryService(primary), {"id": "op-4"}, plan_record(new, release_digest))
    run.phase("drain")
    primary.release()
    other = coordinator("node-c", tmp_path)
    try:
        assert other.try_acquire()  # A new primary: the drain of the old epoch is not an order anymore.
        rollout.Follower(service).tick()
        assert not Path(service.config["maintenance_marker"]).exists()
    finally:
        other.close()


def test_a_node_that_missed_the_update_installs_the_cluster_release_before_starting(cluster):
    primary, service, new, release_digest = cluster
    primary.publish_release(str(new), 2, release_digest)
    follower = rollout.Follower(service)
    follower.tick()
    assert service.active.name == "2-plan"
    assert [entry[0] for entry in service.log] == ["cutover", "start", "health", "active"]
    follower.tick()  # Already on the cluster's release and started: nothing more.
    assert [entry[0] for entry in service.log].count("start") == 1


def test_a_tampered_archive_is_refused(cluster, tmp_path):
    primary, service, new, release_digest = cluster
    (new / "manage.py").write_text("print('tampered')\n", encoding="utf-8")
    with pytest.raises(AgentError, match="follower_release_mismatch"):
        rollout.install_release(service, "2-plan", release_digest)


def test_unsafe_archive_members_are_refused(tmp_path):
    archive = tmp_path / "evil.tar"
    payload = tmp_path / "payload"
    payload.write_text("x")
    with tarfile.open(archive, "w") as tar:
        tar.add(payload, arcname="../escape")
    with tarfile.open(archive) as tar, pytest.raises(AgentError, match="unsafe_release_archive"):
        rollout._safe_members(tar, "2-plan")


def test_rolling_switches_one_node_at_a_time_and_only_on_its_turn(cluster, monkeypatch):
    primary, service, new, release_digest = cluster
    monkeypatch.setattr(rollout.time, "sleep", lambda seconds: None)  # The drain grace of the test.
    service.config["drain_marker"] = str(Path(service.config["releases_root"]).parent / "node-b.drain")
    install = rollout.begin(PrimaryService(primary), {"id": "op-r"}, plan_record(new, release_digest), mode="rolling")
    follower = rollout.Follower(service)
    install.phase("prepare")
    follower.tick()
    assert install.answers()["node-b"] == "prepared"
    # Another node's turn: this follower does nothing.
    primary._lock_conn.execute(
        "UPDATE skeleton_deploy.rollout SET phase = 'rolling', current_node = 'node-x' WHERE operation_id = 'op-r'"
    )
    follower.tick()
    assert not any(entry[0] == "cutover" for entry in service.log)
    stop = threading.Event()
    thread = threading.Thread(target=follow_until, args=(service, stop), daemon=True)
    thread.start()
    try:
        install.wait_node("rolling", "node-b", timeout=10, code="rolling_node_failed")
    finally:
        stop.set()
        thread.join(5)
    assert service.active.name == "2-plan"
    assert [entry[0] for entry in service.log if entry[0] != "active"][-4:] == ["drain", "cutover", "start", "health"]
    assert not Path(service.config["drain_marker"]).exists()  # Back in the balancer.


def test_a_node_that_fails_its_turn_goes_back_and_names_itself(cluster, monkeypatch):
    primary, service, new, release_digest = cluster
    monkeypatch.setattr(rollout.time, "sleep", lambda seconds: None)

    def broken(service, inventory):
        raise AgentError("application_smoke_failed", 409)

    monkeypatch.setattr(execution, "health", broken)
    run = rollout.begin(PrimaryService(primary), {"id": "op-f"}, plan_record(new, release_digest), mode="rolling")
    rollout.install_release(service, "2-plan", release_digest)
    stop = threading.Event()
    thread = threading.Thread(target=follow_until, args=(service, stop), daemon=True)
    thread.start()
    try:
        with pytest.raises(AgentError) as failed:
            run.wait_node("rolling", "node-b", timeout=10, code="rolling_node_failed")
    finally:
        stop.set()
        thread.join(5)
    assert failed.value.details == [{"code": "application_smoke_failed", "component": "node-b"}]
    assert service.active.name == "node-b-old"
    assert service.log[-2:] == [("cutover", "node-b-old"), ("start", "node-b-old")]
