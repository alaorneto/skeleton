"""The agent's pg_dump/pg_restore environment for a DSN with several hosts (failover, #676)."""

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent.backends import host_lists  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402

pytestmark = pytest.mark.posix


def test_the_agent_builds_host_lists_for_pg_dump():
    assert host_lists("owner:pw@db-a:5432,db-b:5433") == ("db-a,db-b", "5432,5433")
    assert host_lists("owner@db-a") == ("db-a", "5432")
    assert host_lists("owner:pw@[::1]:5433,db-b") == ("::1,db-b", "5433,5432")
    with pytest.raises(AgentError):
        host_lists("owner@db-a:port")
