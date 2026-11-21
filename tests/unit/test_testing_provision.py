"""python -m skeleton.testing.database provision (#520): the wheel provisions a disposable test database."""

import os
from pathlib import Path

import pytest

from skeleton.testing.database import BOOTSTRAP_SQL, main, provision

ROOT = Path(__file__).resolve().parents[2]


def test_the_shipped_bootstrap_is_identical_to_the_deploy_reference():
    assert BOOTSTRAP_SQL.read_bytes() == (ROOT / "deploy" / "postgres" / "bootstrap.sql").read_bytes()


@pytest.mark.parametrize(
    "name", ["acme_test", "acme_skeleton_test_x", 'acme"; DROP_skeleton_test', "Acme_skeleton_test", ""]
)
def test_a_database_outside_the_suffix_is_refused_before_connecting(name):
    with pytest.raises(RuntimeError, match="_skeleton_test"):
        provision("postgres://never-used.invalid/postgres", name)


def test_the_administrative_url_is_required(monkeypatch, capsys):
    monkeypatch.delenv("SKELETON_TEST_ADMIN_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as error:
        main(["provision", "--database", "acme_skeleton_test"])
    assert error.value.code == 2 and "administrative URL" in capsys.readouterr().err


def test_a_connection_failure_never_echoes_the_administrative_url(capsys):
    url = "postgres://operator:do-not-print@127.0.0.1:1/postgres?connect_timeout=1"
    with pytest.raises(SystemExit) as error:
        main(["provision", "--admin-url", url, "--database", "acme_skeleton_test"])
    captured = capsys.readouterr()
    assert error.value.code == 1 and "do-not-print" not in captured.err + captured.out


@pytest.mark.skipif(
    not os.environ.get("SKELETON_TEST_ADMIN_DATABASE_URL"),
    reason="Needs SKELETON_TEST_ADMIN_DATABASE_URL (an operator connection to a disposable server).",
)
def test_provision_is_idempotent_against_postgresql():
    import psycopg
    from psycopg.conninfo import make_conninfo

    admin = os.environ["SKELETON_TEST_ADMIN_DATABASE_URL"]
    database = f"provision{os.getpid()}_skeleton_test"
    try:
        assert provision(admin, database) is True
        assert provision(admin, database) is False
        with psycopg.connect(make_conninfo(admin, dbname=database)) as connection:
            assert connection.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'skeleton'").fetchone()
            granted = connection.execute(
                "SELECT has_database_privilege('skeleton_owner', current_database(), 'CREATE')"
            ).fetchone()
            assert granted == (True,)
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{database}"')


def test_the_bootstrap_copy_and_the_host_project_are_package_data():
    import tomllib
    from fnmatch import fnmatch

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = pyproject["tool"]["setuptools"]
    assert any(fnmatch("bootstrap.sql", pattern) for pattern in setuptools["package-data"]["*"])
    assert "skeleton.*" in setuptools["packages"]["find"]["include"]  # skeleton.testing.host is a package
    assert (ROOT / "skeleton" / "testing" / "host" / "__init__.py").is_file()


def test_workers_provision_one_database_per_worker_after_the_base(monkeypatch, capsys):
    """--workers N provisions <base>_gw0..N-1_skeleton_test the same way as the base (#573)."""
    from skeleton.testing import database

    provisioned = []
    monkeypatch.setattr(database, "provision", lambda url, name, *, passwords: provisioned.append(name) or True)
    assert (
        main(
            ["provision", "--admin-url", "postgres://x/postgres", "--database", "acme_skeleton_test", "--workers", "2"]
        )
        == 0
    )
    assert provisioned == ["acme_skeleton_test", "acme_gw0_skeleton_test", "acme_gw1_skeleton_test"]
    assert "acme_gw1_skeleton_test: created" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main(
            ["provision", "--admin-url", "postgres://x/postgres", "--database", "acme_skeleton_test", "--workers", "-1"]
        )
    assert error.value.code == 2
