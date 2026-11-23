"""The primary's side of an update across a cluster (#681): what each follower failure leaves.

The followers' answers come from a fake ``Rollout``; their side of every phase is proven against
PostgreSQL in ``tests/integration/test_agent_rollout.py``. Here the real ``execute``, ``finish``
and ``recover`` run on the primary with a recording backend.
"""

from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import execution  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402
from tests.unit.test_deployment_journal import approved_deployment  # noqa: E402, F401 - the fixture

pytestmark = pytest.mark.posix


class Cluster:
    """The followers, as the primary sees them: each phase answers, or fails with ``code``."""

    def __init__(self, log, fail=None):
        self.log, self.fail, self.nodes = log, fail, ["node-b"]

    def wait(self, phase, *, timeout, code):
        self.log.append(("wait", phase))
        if phase == self.fail:
            raise AgentError(code, 409, [{"code": "node_timeout", "component": "node-b"}])

    def phase(self, phase):
        self.log.append(("phase", phase))

    def wait_node(self, phase, node, *, timeout, code):
        self.log.append(("turn", phase, node))
        if phase == self.fail:
            raise AgentError(code, 409, [{"code": "application_smoke_failed", "component": node}])


class Coordinator:
    is_primary = True

    def assert_primary(self):
        pass

    def publish_release(self, *args):
        pass


@pytest.fixture
def primary(approved_deployment, monkeypatch, tmp_path):  # noqa: F811 - the imported fixture
    service, values, pending = approved_deployment
    log = []
    record = service.store.get("plans", values["plan_id"])
    record["inventory"]["core"] = {"version": "0.4.0"}
    service.store.put("plans", values["plan_id"], record)
    service.coordinator = Coordinator()
    service.rollout = None
    previous = Path(record["previous_release"])

    def recorder(name):
        return lambda *args, **kwargs: log.append((name, *(str(argument) for argument in args[:2])))

    service.backend.drain = recorder("drain")
    service.backend.backup = lambda *args: log.append(("backup",)) or str(tmp_path / "backup.json")
    service.backend.manage = lambda release, argv, profile, code: log.append(("manage", argv[0]))
    service.backend.cutover = recorder("cutover")
    service.backend.start = recorder("start")
    service.backend.restore = recorder("restore")
    monkeypatch.setattr(execution, "health", lambda service, inventory: log.append(("health",)))
    cluster = {"current": Cluster(log)}
    monkeypatch.setattr(execution.rollout, "begin", lambda *args, **kwargs: cluster["current"])
    monkeypatch.setattr(execution.rollout, "current", lambda service, operation: cluster["current"])

    def run(fail=None, rolling=False):
        if rolling:
            stored = service.store.get("plans", values["plan_id"])
            stored["plan"]["rollout_mode"] = "rolling"  # As the preview of a patch without migrations.
            service.store.put("plans", values["plan_id"], stored)
        cluster["current"] = Cluster(log, fail)
        service.submit(**values)
        worker = pending.pop()
        worker.target(*worker.args)
        return service.store.get("operations", values["operation_id"])

    return service, run, log, cluster, previous


def marker(service):
    return Path(service.config["maintenance_marker"]).exists()


def test_a_window_update_prepares_everywhere_backs_up_after_the_drain_and_migrates_once(primary):
    service, run, log, _, _ = primary
    operation = run()
    assert operation["state"] == "succeeded" and operation["stage"] == "ready"
    steps = [entry[0] if entry[0] != "wait" else f"wait:{entry[1]}" for entry in log]
    assert steps.index("wait:prepare") < steps.index("drain") < steps.index("wait:drain") < steps.index("backup")
    migrations = [entry for entry in log if entry == ("manage", "migrate")]
    assert len(migrations) == 1 and steps.index("backup") < log.index(("manage", "migrate"))
    assert steps.index("health") < steps.index("wait:cutover") < steps.index("wait:resume")
    assert ("phase", "done") in log and not marker(service)


def test_a_follower_that_fails_the_prepare_leaves_no_maintenance_and_no_migration(primary):
    service, run, log, _, _ = primary
    operation = run(fail="prepare")
    assert (operation["state"], operation["error"]) == ("failed", "follower_prepare_failed")
    assert log == [("wait", "prepare")]  # No drain, no backup, no migration.
    assert not marker(service)
    service.ensure_available()  # Nothing to recover: the cluster takes the next update.


def test_a_follower_that_fails_the_drain_brings_every_node_back_without_a_migration(primary):
    service, run, log, _, previous = primary
    operation = run(fail="drain")
    assert (operation["state"], operation["error"]) == ("failed", "follower_drain_failed")
    assert ("phase", "abort") in log and ("start", str(previous), operation["id"]) in log
    assert not any(entry[0] in ("backup", "manage") for entry in log)
    assert not marker(service)  # The primary serves its release again, like the followers.
    service.ensure_available()


def test_a_follower_that_fails_the_cutover_keeps_the_maintenance_until_recover_completes(primary):
    service, run, log, cluster, _ = primary
    operation = run(fail="cutover")
    assert (operation["state"], operation["error"]) == ("failed", "follower_cutover_failed")
    assert log.count(("manage", "migrate")) == 1 and marker(service)  # Migrated: the maintenance stays.
    with pytest.raises(AgentError, match="recovery_or_operation_pending"):
        service.ensure_available()
    cluster["current"] = Cluster(log)  # The node came back (or was forgotten): the cutover answers now.
    recovered = service.recover(operation["id"], "complete")
    assert recovered["state"] == "succeeded" and recovered["stage"] == "ready"
    assert log[-1] == ("phase", "done") and ("wait", "resume") in log and not marker(service)


def test_a_rolling_update_that_fails_on_a_follower_keeps_the_primary_on_the_new_release(primary):
    """#682: the nodes already switched stay; recover complete finishes the operation."""
    service, run, log, cluster, previous = primary
    operation = run(fail="rolling", rolling=True)
    assert (operation["state"], operation["error"]) == ("failed", "rolling_node_failed")
    release = operation["release"]
    assert ("cutover", release) in log and ("cutover", str(previous)) not in log  # The primary stays.
    assert ("turn", "rolling", "node-b") in log and not marker(service)  # Never a maintenance window.
    assert not any(entry == ("manage", "migrate") for entry in log)
    cluster["current"] = Cluster(log)
    recovered = service.recover(operation["id"], "complete")
    assert (recovered["state"], recovered["stage"]) == ("succeeded", "ready") and not marker(service)
