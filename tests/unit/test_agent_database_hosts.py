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


class Config(dict):
    def __init__(self, url):
        super().__init__(storage_root="/agent/state")
        self.url = url

    def environment(self, profile, release):
        assert profile == "migration"
        return {"MIGRATION_DATABASE_URL": self.url}


def test_the_backup_environment_of_a_failover_dsn_targets_the_writable_primary():
    from deployment_agent.backends import Backend

    url = "postgres://owner:pw%21@db-a:5432,db-b:5433/skeleton?target_session_attrs=read-write&sslmode=verify-full"
    environment = Backend(Config(url), store=None).pg_environment("/releases/r-1")
    assert {key: environment[key] for key in ("PGHOST", "PGPORT", "PGTARGETSESSIONATTRS", "PGSSLMODE")} == {
        "PGHOST": "db-a,db-b",
        "PGPORT": "5432,5433",
        "PGTARGETSESSIONATTRS": "read-write",
        "PGSSLMODE": "verify-full",
    }
    assert (environment["PGDATABASE"], environment["PGUSER"], environment["PGPASSWORD"]) == ("skeleton", "owner", "pw!")
    with pytest.raises(AgentError, match="unsupported_backup_database_option"):
        Backend(Config("postgres://owner@db-a/skeleton?application_name=x"), store=None).pg_environment("/r")
