"""PostgreSQL with failover and a transaction pooler (#676): host lists, pooler settings and checks."""

import environ
import pytest

from skeleton.conf.base import database_config
from skeleton.setup.database import DatabaseCheckError, hosts_of, parse


def env_with(values):
    env = environ.Env()
    env.ENVIRON = values
    return env


FAILOVER = "postgres://runtime:pw@db-a:5432,db-b:5433/skeleton?target_session_attrs=read-write"


def test_the_setup_reads_every_host_of_a_failover_dsn():
    assert hosts_of(parse(FAILOVER)) == [("db-a", 5432), ("db-b", 5433)]
    assert hosts_of(parse("postgres://runtime:pw@db-a,db-b/skeleton")) == [("db-a", 5432), ("db-b", 5432)]
    assert hosts_of(parse("postgres://runtime:pw@db-a:6432/skeleton")) == [("db-a", 6432)]
    with pytest.raises(DatabaseCheckError):
        hosts_of({"HOST": "db-a,db-b,db-c", "PORT": "5432,5433"})


def test_one_reachable_host_is_enough_for_the_setup(monkeypatch):
    from skeleton.setup import database

    calls = []
    monkeypatch.setattr(
        database,
        "_network_problem",
        lambda host, port: None if host == "db-b" else DatabaseCheckError("down", code="refused"),
    )

    class Connected(Exception):
        pass

    def connect(**kwargs):
        calls.append(kwargs)
        raise Connected

    import psycopg

    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(Connected):
        database._connect(FAILOVER)
    assert calls[0]["host"] == "db-a,db-b" and calls[0]["target_session_attrs"] == "read-write"
    monkeypatch.setattr(database, "_network_problem", lambda host, port: DatabaseCheckError("down", code="refused"))
    with pytest.raises(DatabaseCheckError) as refused:
        database._connect(FAILOVER)
    assert refused.value.code == "refused"


def test_the_pooler_drops_the_startup_options_and_server_side_cursors():
    direct = database_config(env_with({"DATABASE_URL": FAILOVER}), "DATABASE_URL")
    assert direct["OPTIONS"]["options"] == "-c search_path=skeleton,public"
    assert direct["CONN_MAX_AGE"] == 60 and "DISABLE_SERVER_SIDE_CURSORS" not in direct
    assert direct["HOST"] == "db-a,db-b" and direct["OPTIONS"]["target_session_attrs"] == "read-write"
    pooled = database_config(
        env_with({"DATABASE_URL": FAILOVER, "DATABASE_TRANSACTION_POOLER": "true", "DATABASE_CONN_MAX_AGE": "0"}),
        "DATABASE_URL",
    )
    assert "options" not in pooled["OPTIONS"] and pooled["DISABLE_SERVER_SIDE_CURSORS"] is True
    assert pooled["CONN_MAX_AGE"] == 0 and pooled["CONN_HEALTH_CHECKS"] is False
    owner = database_config(
        env_with({"MIGRATION_DATABASE_URL": FAILOVER, "DATABASE_TRANSACTION_POOLER": "true"}),
        "MIGRATION_DATABASE_URL",
        conn_max_age=0,
        pooler=False,
    )
    assert owner["OPTIONS"]["options"].startswith("-c search_path")


def test_the_catalog_validates_the_new_keys():
    from skeleton.instance.schema import CATALOG

    assert CATALOG["database_transaction_pooler"].clean("TRUE") == "true"
    assert CATALOG["database_conn_max_age"].clean("0") == "0"
    with pytest.raises(ValueError):
        CATALOG["database_conn_max_age"].clean("-1")


def test_several_hosts_without_target_session_attrs_warn():
    from skeleton.system.checks.environment import multi_host_problems

    assert multi_host_problems({"default": {"HOST": "db-a", "OPTIONS": {}}}) == []
    assert [item.id for item in multi_host_problems({"default": {"HOST": "db-a,db-b", "OPTIONS": {}}})] == [
        "skeleton.W050"
    ]
    assert multi_host_problems({"default": {"HOST": "a,b", "OPTIONS": {"target_session_attrs": "read-write"}}}) == []


class Cursor:
    def __init__(self, path):
        self.path = path

    def execute(self, sql):
        assert sql == "SHOW search_path"

    def fetchone(self):
        return (self.path,)


def test_behind_the_pooler_the_role_needs_its_search_path():
    from skeleton.system.checks.environment import pooler_search_path_problems

    assert pooler_search_path_problems("default", Cursor("skeleton, public")) == []
    assert [item.id for item in pooler_search_path_problems("default", Cursor('"$user", public'))] == ["skeleton.E050"]
