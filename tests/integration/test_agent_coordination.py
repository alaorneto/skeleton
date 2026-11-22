"""The primary of a cluster of agents, against PostgreSQL (#678): lock, epoch, handover and roles.

Two coordinators in the same process stand for two candidate nodes; the owner connection is the
candidates' credential and ``TEST_AGENT_DATABASE_URL`` the followers' (role ``skeleton_agent``).
Schema ``skeleton_deploy`` comes from ``deploy/postgres/bootstrap.sql``; each test empties its
tables. The skeleton schema is not touched, so no ``skeleton_db`` fixture is needed.
"""

import os
from urllib.parse import urlsplit

import psycopg
import pytest

from deployment_agent import coordination
from deployment_agent.coordination import ClusterSettings, Coordinator
from deployment_agent.storage import AgentError

OWNER = os.environ.get("TEST_MIGRATION_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not OWNER, reason="TEST_MIGRATION_DATABASE_URL is required.")


def agent_url():
    """The followers' role, on the same (worker) database as the owner."""
    url = os.environ.get("TEST_AGENT_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_AGENT_DATABASE_URL is required for the followers' role.")
    return urlsplit(url)._replace(path=urlsplit(OWNER).path).geturl()


def runtime_url():
    return os.environ["TEST_DATABASE_URL"]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make(tmp_path):
    made = []

    def factory(node_id, *, role="candidate", preferred=False, url=None, storage=None, clock=None):
        settings = ClusterSettings(
            node_id=node_id,
            role=role,
            cluster_url=f"https://{node_id}.cluster.test:9443",
            preferred=preferred,
            database_url=url or OWNER,
        )
        coordinator = Coordinator(settings, storage_root=storage or tmp_path / "shared", clock=clock or Clock())
        made.append(coordinator)
        return coordinator

    setup = factory("setup")
    setup.ensure_schema()
    setup._execute('DELETE FROM skeleton_deploy."primary"')
    setup._execute("DELETE FROM skeleton_deploy.node")
    yield factory
    for coordinator in made:
        coordinator.close()


def test_exactly_one_candidate_holds_the_lock_and_the_next_takes_it_when_it_dies(make):
    a, b = make("node-a", preferred=True), make("node-b")
    assert a.try_acquire() and a.epoch == 1
    assert not b.try_acquire() and b.epoch is None
    assert a.primary()["node_id"] == "node-a"
    # The primary's session dies (crash, network): PostgreSQL frees the lock.
    with psycopg.connect(OWNER, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (a._lock_conn.info.backend_pid,))
    assert b.try_acquire() and b.epoch == 2
    # The old primary comes back: fenced by the epoch before any database step.
    with pytest.raises(AgentError) as fenced:
        a.assert_primary()
    assert fenced.value.code == "not_primary" and not a.is_primary
    b.assert_primary()


def test_a_frozen_primary_that_lost_the_epoch_refuses(make):
    a, b = make("node-a"), make("node-b")
    assert a.try_acquire()
    a.release()  # Lost the lock while it looked away.
    assert b.try_acquire() and b.epoch == 2
    a.epoch = 1  # It still believes it is the primary of epoch 1.
    a._lock_conn = psycopg.connect(OWNER, autocommit=True)
    with pytest.raises(AgentError, match="not_primary"):
        a.assert_primary()


def test_a_planned_handover_moves_the_lock_only_when_idle(make):
    a, b = make("node-a"), make("node-b")
    follower = make("node-c", role="follower")
    for node in (a, b, follower):
        node.heartbeat()
    assert a.try_acquire()
    with pytest.raises(AgentError, match="cluster_busy"):
        b.request_handover("node-b", busy=True)
    with pytest.raises(AgentError, match="handover_target_invalid"):
        b.request_handover("node-c", busy=False)  # A follower never becomes primary.
    with pytest.raises(AgentError, match="handover_target_invalid"):
        b.request_handover("node-a", busy=False)  # Already the primary.
    assert b.request_handover("node-b", busy=False)["to"] == "node-b"
    assert not a.renew() and not a.is_primary  # The primary gives the lock back on its next renewal.
    assert b.try_acquire() and b.epoch == 2
    assert b.primary()["handover_to"] is None


def test_the_first_start_waits_unless_preferred_and_a_handover_waits_for_its_target(make):
    clock = Clock()
    plain = make("node-a", clock=clock)
    preferred = make("node-b", preferred=True, clock=clock)
    assert preferred._may_try(None, clock())
    assert not plain._may_try(None, clock())
    clock.now += coordination.FIRST_WAIT_S
    assert plain._may_try(None, clock())
    handing = {"node_id": "node-b", "handover_to": "node-x", "epoch": 3}
    other = make("node-c", clock=Clock())
    assert not other._may_try(handing, other._clock())


def test_followers_report_through_their_own_role_and_see_nothing_else(make):
    follower = make("node-f", role="follower", url=agent_url())
    follower.heartbeat(release="/releases/r-1", generation=4)
    assert [node["release"] for node in follower.nodes()] == ["r-1"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        follower._execute('UPDATE skeleton_deploy."primary" SET epoch = 99')
    with psycopg.connect(agent_url(), autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT 1 FROM skeleton.sk_accounts_user LIMIT 1")
    with psycopg.connect(runtime_url(), autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT 1 FROM skeleton_deploy.node LIMIT 1")
    assert not follower.try_acquire()  # Never the lock, whatever the timing.


def test_the_primary_cannot_be_forgotten(make):
    a, gone = make("node-a"), make("node-z", role="follower")
    a.heartbeat()
    gone.heartbeat()
    assert a.try_acquire()
    with pytest.raises(AgentError, match="cannot_forget_primary"):
        a.forget("node-a")
    a.forget("node-z")
    assert [node["node_id"] for node in a.nodes()] == ["node-a"]


def test_two_candidates_need_the_same_storage_root(make, tmp_path, monkeypatch):
    monkeypatch.setattr(coordination, "STORAGE_GRACE_S", -1)
    a = make("node-a", storage=tmp_path / "a")
    b = make("node-b", storage=tmp_path / "b")
    a.heartbeat()
    b.heartbeat()
    # Each writes its mark in its own storage_root and misses the other's.
    assert a.storage_problem() == b.storage_problem() == "cluster_storage_not_shared"
    shared_a, shared_b = make("node-c", storage=tmp_path / "s"), make("node-d", storage=tmp_path / "s")
    a.forget("node-a")
    a.forget("node-b")
    shared_a.heartbeat()
    shared_b.heartbeat()
    shared_a.storage_problem()
    assert shared_b.storage_problem() is None


def test_status_names_the_primary_and_every_node_without_urls(make):
    a, follower = make("node-a"), make("node-f", role="follower")
    a.heartbeat(release="/releases/gen-2", generation=2)
    follower.heartbeat()
    assert a.try_acquire()
    status = a.status()
    assert status["mode"] == "cluster" and status["is_primary"] and status["primary"]["epoch"] == 1
    assert {node["node_id"] for node in status["nodes"]} == {"node-a", "node-f"}
    assert "cluster_url" not in status["nodes"][0]
