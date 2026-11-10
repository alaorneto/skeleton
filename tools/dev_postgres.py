"""A disposable local PostgreSQL for development and tests, without Docker (#145).

    python tools/dev_postgres.py start            # init (first time), start and provision
    eval "$(python tools/dev_postgres.py env)"    # POSIX shells
    python tools/dev_postgres.py env --shell powershell | Invoke-Expression
    python tools/dev_postgres.py stop

The server comes from ``pgserver`` (PostgreSQL binaries in a wheel). ``pgserver`` publishes
wheels only for CPython 3.12, so this script runs it through ``uv`` in an isolated 3.12
environment instead of the project's virtualenv; nothing is added to the project locks.
Data lives in ``var/dev-postgres`` (git-ignored). The database ``local_skeleton_test`` is
provisioned with ``deploy/postgres/bootstrap.sql`` and local-only passwords, the same layout
as the CI: migrations as ``skeleton_owner``, assertions as ``skeleton_runtime``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "var" / "dev-postgres"
STATE = DATA / "state.json"
DATABASE = "local_skeleton_test"
PASSWORDS = {"skeleton_owner": "local-owner", "skeleton_runtime": "local-runtime"}
PGSERVER = "pgserver==0.1.4"

# Runs inside the uv environment (Python 3.12 + pgserver + tzdata); prints one JSON line.
SERVER = r"""
import json, shutil, subprocess, sys
from pathlib import Path
import pgserver, tzdata
from pgserver._commands import POSTGRES_BIN_PATH

data, bootstrap, database, owner_pw, runtime_pw = sys.argv[1:6]
share = Path(pgserver.__file__).parent / "pginstall" / "share" / "postgresql" / "timezone"
if not (share / "UTC").exists():
    # Some pgserver builds ship without time zone data: copy it from the tzdata package.
    shutil.copytree(Path(tzdata.__file__).parent / "zoneinfo", share, dirs_exist_ok=True)
server = pgserver.get_server(Path(data) / "pgdata", cleanup_mode=None)
info = server.get_postmaster_info()
host = info.socket_dir or "127.0.0.1"
exists = server.psql(f"SELECT 1 FROM pg_database WHERE datname='{database}';")
if "1 row" not in exists:
    server.psql(f"CREATE DATABASE {database};")
    script = Path(data) / "bootstrap.sql"
    script.write_text(
        Path(bootstrap).read_text(encoding="utf-8")
        + f"\nALTER ROLE skeleton_owner PASSWORD '{owner_pw}';\n"
        + f"ALTER ROLE skeleton_runtime PASSWORD '{runtime_pw}';\n"
        + f"GRANT CREATE ON DATABASE {database} TO skeleton_owner;\n",
        encoding="utf-8",
    )
    psql = POSTGRES_BIN_PATH / ("psql.exe" if sys.platform == "win32" else "psql")
    subprocess.run(
        [str(psql), "-q", "-v", "ON_ERROR_STOP=1", "-h", host, "-p", str(info.port), "-U", "postgres",
         "-d", database, "-f", str(script)],
        check=True,
    )
print(json.dumps({"host": host, "port": info.port, "bin": str(POSTGRES_BIN_PATH), "pgdata": str(Path(data) / "pgdata")}))
"""

STOP = r"""
import subprocess, sys
from pathlib import Path
from pgserver._commands import POSTGRES_BIN_PATH
pg_ctl = POSTGRES_BIN_PATH / ("pg_ctl.exe" if sys.platform == "win32" else "pg_ctl")
subprocess.run([str(pg_ctl), "-D", sys.argv[1], "stop", "-m", "fast"], check=False)
"""


def _uv(code: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("tools/dev_postgres.py needs uv (https://docs.astral.sh/uv/) to run pgserver on Python 3.12.")
    command = [uv, "run", "--quiet", "--no-project", "--python", "3.12", "--with", PGSERVER, "--with", "tzdata"]
    return subprocess.run([*command, "python", "-c", code, *arguments], check=True, capture_output=True, text=True)


def _dsn(user: str, state: dict) -> str:
    host = state["host"]
    location = quote(host, safe="") if "/" in host or "\\" in host else host
    return f"postgres://{user}:{PASSWORDS[user]}@{location}:{state['port']}/{DATABASE}"


def environment(state: dict) -> dict[str, str]:
    binary = Path(state["bin"]) / ("pg_dump.exe" if os.name == "nt" else "pg_dump")
    return {
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "SKELETON_TEST_ALLOW_SCHEMA_RESET": "1",
        "TEST_DATABASE_URL": _dsn("skeleton_runtime", state),
        "TEST_MIGRATION_DATABASE_URL": _dsn("skeleton_owner", state),
        "SKELETON_TEST_PG_DUMP": str(binary),
    }


def start() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    result = _uv(SERVER, str(DATA), str(ROOT / "deploy" / "postgres" / "bootstrap.sql"), DATABASE, *PASSWORDS.values())
    state = json.loads(result.stdout.strip().splitlines()[-1])
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def load_state() -> dict:
    if not STATE.exists():
        raise SystemExit("No local PostgreSQL yet: run `python tools/dev_postgres.py start`.")
    return json.loads(STATE.read_text(encoding="utf-8"))


def render(variables: dict[str, str], shell: str) -> str:
    if shell == "powershell":
        return "\n".join(f"$env:{name} = '{value}'" for name, value in variables.items())
    return "\n".join(f"export {name}='{value}'" for name, value in variables.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="Initialize, start and provision the local server.")
    sub.add_parser("stop", help="Stop the local server (data is kept).")
    shell = sub.add_parser("env", help="Print the test variables for a shell.")
    shell.add_argument("--shell", choices=("posix", "powershell"), default="powershell" if os.name == "nt" else "posix")
    arguments = parser.parse_args(argv)
    if arguments.command == "start":
        state = start()
        print(f"PostgreSQL local em {state['host']}:{state['port']} (dados em {DATA}).")
        print(render(environment(state), "powershell" if os.name == "nt" else "posix"))
    elif arguments.command == "env":
        print(render(environment(load_state()), arguments.shell))
    else:
        state = load_state()
        _uv(STOP, state["pgdata"])
        print("PostgreSQL local parado; os dados continuam em var/dev-postgres.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
