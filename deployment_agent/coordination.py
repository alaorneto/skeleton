"""Coordination of the deployment agents of a cluster in PostgreSQL (#678).

Several application nodes run one agent each. Exactly one of them, the **primary**, migrates and
coordinates; the others, the **followers**, prepare and switch their own releases (#681). This
module defines who the primary is and how it changes hands:

- Only a **candidate** node (``cluster.role = "candidate"``, with the owner credential) can be the
  primary; a ``follower`` never receives the owner and never becomes primary.
- The primary is the candidate that holds the session advisory lock ``LOCK_KEY`` on a dedicated,
  direct connection (never through PgBouncer), plus the current **epoch** of the single row of
  ``skeleton_deploy."primary"``. PostgreSQL releases the lock only when that session ends, so
  there are never two primaries while the first one is alive and connected.
- Every write of the primary to the coordination state carries ``WHERE epoch = <its epoch>``, and
  ``assert_primary`` checks the lock connection and the epoch before each step that changes the
  database: a primary that froze, lost the lock and came back refuses instead of migrating.
- First start: a ``preferred`` candidate tries at once, the others after ``FIRST_WAIT_S``.
- Planned change: ``request_handover`` writes ``handover_to``; the primary releases the lock on its
  next tick and the other candidates give the target ``HANDOVER_WAIT_S`` before trying.
- Loss of the primary: its session ends (TCP keepalives notice in about 30 s), the lock is free and
  the next candidate to try (every ``RETRY_S``) takes it with the next epoch. The agent's journal
  then marks an operation in flight as ``interrupted`` and refuses new ones until the operator's
  ``recover`` (the same procedure as one node): the decision stays human.

The coordination tables live in schema ``skeleton_deploy`` (created by ``deploy/postgres/bootstrap.sql``
and owned by the owner); the candidates create the tables, the followers read and report through
the role ``skeleton_agent``; the web and the runtime role never see them. Only the standard library
and ``psycopg``: no Django here.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .storage import AgentError
from .types import JSON

SCHEMA = "skeleton_deploy"
# A fixed 64-bit key of the cluster's advisory lock: hashtext-independent, the same in every release.
LOCK_KEY = 0x736B5F6465706C79  # "sk_deply"
SCHEMA_VERSION = 1
NODE_ID = re.compile(r"[a-z0-9-]{1,40}")
ROLES = ("candidate", "follower")
RENEW_S = 10.0
RETRY_S = 15.0
FIRST_WAIT_S = 60.0
HANDOVER_WAIT_S = 30.0
ALIVE_S = 60.0
STORAGE_GRACE_S = 60.0
KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 3,
    "tcp_user_timeout": 30000,
    "connect_timeout": 10,
}

DDL = (
    "CREATE TABLE IF NOT EXISTS skeleton_deploy.schema_version (version integer NOT NULL)",
    'CREATE TABLE IF NOT EXISTS skeleton_deploy."primary" ('
    " id integer PRIMARY KEY CHECK (id = 1), node_id text NOT NULL, epoch bigint NOT NULL,"
    " acquired_at timestamptz NOT NULL, renewed_at timestamptz NOT NULL, cluster_url text NOT NULL DEFAULT '',"
    " handover_to text)",
    "CREATE TABLE IF NOT EXISTS skeleton_deploy.node ("
    " node_id text PRIMARY KEY, role text NOT NULL CHECK (role IN ('candidate', 'follower')),"
    " cluster_url text NOT NULL DEFAULT '', release text NOT NULL DEFAULT '', generation integer,"
    " phase text NOT NULL DEFAULT '', phase_epoch bigint, agent_version text NOT NULL DEFAULT '',"
    " registered_at timestamptz NOT NULL DEFAULT now(), heartbeat_at timestamptz NOT NULL DEFAULT now())",
    "REVOKE ALL ON ALL TABLES IN SCHEMA skeleton_deploy FROM PUBLIC",
    'GRANT SELECT ON skeleton_deploy."primary", skeleton_deploy.schema_version TO skeleton_agent',
    "GRANT SELECT, INSERT, UPDATE ON skeleton_deploy.node TO skeleton_agent",
)


@dataclass(frozen=True)
class ClusterSettings:
    node_id: str
    role: str
    cluster_url: str
    preferred: bool
    database_url: str

    @property
    def candidate(self) -> bool:
        return self.role == "candidate"


def validate_cluster(values: JSON) -> None:
    """The ``cluster`` key of ``agent.json`` (#678); absent means a single node, as before."""
    cluster = values.get("cluster")
    if cluster is None:
        return
    if not isinstance(cluster, dict):
        raise AgentError("invalid_cluster_config")
    if not isinstance(cluster.get("node_id"), str) or not NODE_ID.fullmatch(cluster["node_id"]):
        raise AgentError("invalid_cluster_node_id")
    if cluster.get("role") not in ROLES:
        raise AgentError("invalid_cluster_role")
    url = cluster.get("cluster_url", "")
    if not isinstance(url, str) or (url and (urlsplit(url).scheme != "https" or not urlsplit(url).hostname)):
        raise AgentError("invalid_cluster_url")
    if not isinstance(cluster.get("preferred", False), bool):
        raise AgentError("invalid_cluster_config")
    from .cluster_channel import validate_channel

    validate_channel(cluster)
    if cluster["role"] == "follower":
        path = cluster.get("coordination_env_file")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise AgentError("missing_coordination_credentials")


def settings_for(config: Any) -> ClusterSettings | None:
    """The cluster settings of a loaded configuration, with the connection URL of its role."""
    from .config import read_environment

    cluster = config.get("cluster")
    if not cluster:
        return None
    if cluster["role"] == "candidate":
        url = read_environment(config["migration_env_file"]).get("MIGRATION_DATABASE_URL", "")
    else:
        url = read_environment(cluster["coordination_env_file"]).get("COORDINATION_DATABASE_URL", "")
    if not url:
        raise AgentError("missing_coordination_credentials")
    return ClusterSettings(
        node_id=cluster["node_id"],
        role=cluster["role"],
        cluster_url=cluster.get("cluster_url", ""),
        preferred=bool(cluster.get("preferred", False)),
        database_url=url,
    )


def _connect(url: str) -> Any:
    import psycopg
    from psycopg.conninfo import make_conninfo

    return psycopg.connect(make_conninfo(url, **KEEPALIVES), autocommit=True)


class Coordinator:
    """One node's view of the cluster: registration, heartbeat, the primary's lock and epoch."""

    def __init__(
        self,
        settings: ClusterSettings,
        *,
        storage_root: Path,
        agent_version: str = "",
        connect: Callable[[str], Any] = _connect,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.storage_root = Path(storage_root)
        self.agent_version = agent_version
        self._connect = connect
        self._clock = clock
        self._conn: Any = None
        self._lock_conn: Any = None
        self.epoch: int | None = None
        self.started = clock()
        self._last_try: float | None = None
        self._last_renew = 0.0
        self.problem: str | None = None

    # ------------------------------------------------------------------ connections

    def _db(self) -> Any:
        if self._conn is None or self._conn.closed:
            self._conn = self._connect(self.settings.database_url)
        return self._conn

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        try:
            return self._db().execute(sql, params)
        except Exception:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            raise

    def close(self) -> None:
        self.release()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ schema and nodes

    def ensure_schema(self) -> None:
        """Create the coordination tables (candidates, with the owner); idempotent."""
        if not self.settings.candidate:
            return
        connection = self._db()
        with connection.transaction():
            for statement in DDL:
                connection.execute(statement)
            row = connection.execute("SELECT max(version) FROM skeleton_deploy.schema_version").fetchone()
            if row[0] is None:
                connection.execute(
                    "INSERT INTO skeleton_deploy.schema_version (version) VALUES (%s)", (SCHEMA_VERSION,)
                )

    def heartbeat(self, *, release: str = "", generation: int | None = None, phase: str | None = None) -> None:
        """Register this node or refresh its row; ``phase`` only when given."""
        self._execute(
            "INSERT INTO skeleton_deploy.node (node_id, role, cluster_url, release, generation, phase, agent_version)"
            " VALUES (%s, %s, %s, %s, %s, COALESCE(%s, ''), %s)"
            " ON CONFLICT (node_id) DO UPDATE SET role = excluded.role, cluster_url = excluded.cluster_url,"
            " release = excluded.release, generation = excluded.generation,"
            " phase = COALESCE(%s, skeleton_deploy.node.phase), agent_version = excluded.agent_version, heartbeat_at = now()",
            (
                self.settings.node_id,
                self.settings.role,
                self.settings.cluster_url,
                release,
                generation,
                phase,
                self.agent_version,
                phase,
            ),
        )

    def forget(self, node_id: str) -> None:
        primary = self.primary()
        if primary and primary["node_id"] == node_id:
            raise AgentError("cannot_forget_primary", 409)
        self._execute("DELETE FROM skeleton_deploy.node WHERE node_id = %s", (node_id,))

    # ------------------------------------------------------------------ the primary

    @property
    def is_primary(self) -> bool:
        return self.epoch is not None

    def primary(self) -> JSON | None:
        row = self._execute(
            'SELECT node_id, epoch, cluster_url, handover_to, extract(epoch FROM now() - renewed_at) FROM skeleton_deploy."primary"'
            " WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        node_id, epoch, url, handover_to, age = row
        return {
            "node_id": node_id,
            "epoch": int(epoch),
            "cluster_url": url,
            "handover_to": handover_to,
            "renewed_age_s": float(age),
        }

    def try_acquire(self) -> bool:
        """Take the lock and the next epoch; False when another candidate holds it."""
        if not self.settings.candidate or self.is_primary:
            return self.is_primary
        connection = self._connect(self.settings.database_url)
        try:
            acquired = connection.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]
        except Exception:
            connection.close()
            raise
        if not acquired:
            connection.close()
            return False
        try:
            with connection.transaction():
                row = connection.execute(
                    'UPDATE skeleton_deploy."primary" SET node_id = %s, epoch = epoch + 1, acquired_at = now(),'
                    " renewed_at = now(), cluster_url = %s, handover_to = NULL WHERE id = 1 RETURNING epoch",
                    (self.settings.node_id, self.settings.cluster_url),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        'INSERT INTO skeleton_deploy."primary" (id, node_id, epoch, acquired_at, renewed_at, cluster_url)'
                        " VALUES (1, %s, 1, now(), now(), %s) RETURNING epoch",
                        (self.settings.node_id, self.settings.cluster_url),
                    ).fetchone()
        except Exception:
            connection.close()
            raise
        self._lock_conn = connection
        self.epoch = int(row[0])
        self._last_renew = self._clock()
        return True

    def renew(self) -> bool:
        """Refresh ``renewed_at``; False (and released) when the epoch moved or a handover asks for it."""
        if not self.is_primary:
            return False
        try:
            row = self._lock_conn.execute(
                'UPDATE skeleton_deploy."primary" SET renewed_at = now() WHERE id = 1 AND epoch = %s AND node_id = %s'
                " RETURNING handover_to",
                (self.epoch, self.settings.node_id),
            ).fetchone()
        except Exception:  # noqa: BLE001 - a lost session is a lost lock: become a follower
            self.release()
            return False
        if row is None or (row[0] and row[0] != self.settings.node_id):
            self.release()
            return False
        self._last_renew = self._clock()
        return True

    def release(self) -> None:
        """Give the lock back (or let the closed session do it) and become a follower."""
        connection, self._lock_conn, self.epoch = self._lock_conn, None, None
        if connection is None:
            return
        try:
            connection.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        except Exception:  # noqa: BLE001, S110 - closing the session releases the lock anyway
            pass
        connection.close()

    def assert_primary(self) -> None:
        """Before every step that changes the database: still the lock holder, still the epoch."""
        if not self.is_primary:
            raise AgentError("not_primary", 409)
        try:
            self._lock_conn.execute("SELECT 1").fetchone()
            row = self._lock_conn.execute(
                'SELECT epoch, node_id FROM skeleton_deploy."primary" WHERE id = 1'
            ).fetchone()
        except Exception:  # noqa: BLE001 - a lost session is a lost lock: refuse the step
            self.release()
            raise AgentError("not_primary", 409) from None
        if row is None or int(row[0]) != self.epoch or row[1] != self.settings.node_id:
            self.release()
            raise AgentError("not_primary", 409)

    def request_handover(self, target: str, *, busy: bool) -> JSON:
        """Ask the primary to give the lock to ``target`` (an alive candidate), only when idle."""
        if busy:
            raise AgentError("cluster_busy", 409)
        primary = self.primary()
        nodes = {node["node_id"]: node for node in self.nodes()}
        node = nodes.get(target)
        if (
            primary is None
            or node is None
            or node["role"] != "candidate"
            or not node["alive"]
            or target == primary["node_id"]
        ):
            raise AgentError("handover_target_invalid", 409)
        self._execute(
            'UPDATE skeleton_deploy."primary" SET handover_to = %s WHERE id = 1 AND epoch = %s',
            (target, primary["epoch"]),
        )
        return {"state": "requested", "from": primary["node_id"], "to": target, "epoch": primary["epoch"]}

    # ------------------------------------------------------------------ status

    def nodes(self) -> list[JSON]:
        rows = self._execute(
            "SELECT node_id, role, cluster_url, release, generation, phase, phase_epoch, agent_version,"
            " extract(epoch FROM now() - heartbeat_at), extract(epoch FROM now() - registered_at)"
            " FROM skeleton_deploy.node ORDER BY node_id"
        ).fetchall()
        return [
            {
                "node_id": node_id,
                "role": role,
                "cluster_url": url,
                "release": Path(release).name if release else "",
                "generation": generation,
                "phase": phase,
                "phase_epoch": phase_epoch,
                "agent_version": version,
                "heartbeat_age_s": float(age),
                "registered_age_s": float(registered),
                "alive": float(age) < ALIVE_S,
            }
            for node_id, role, url, release, generation, phase, phase_epoch, version, age, registered in rows
        ]

    def status(self) -> JSON:
        return {
            "mode": "cluster",
            "node_id": self.settings.node_id,
            "role": self.settings.role,
            "is_primary": self.is_primary,
            "primary": self.primary(),
            "nodes": [{key: value for key, value in node.items() if key != "cluster_url"} for node in self.nodes()],
            "problem": self.problem,
        }

    # ------------------------------------------------------------------ shared storage of the candidates

    def storage_problem(self) -> str | None:
        """Two candidates need the same ``storage_root`` (journal, plans, uploads, backups)."""
        if not self.settings.candidate:
            return None
        marks = self.storage_root / ".cluster"
        marks.mkdir(parents=True, exist_ok=True, mode=0o700)
        (marks / self.settings.node_id).write_text(self.settings.node_id, encoding="utf-8")
        for node in self.nodes():
            if (
                node["role"] == "candidate"
                and node["node_id"] != self.settings.node_id
                and node["alive"]
                and node["registered_age_s"] > STORAGE_GRACE_S
                and not (marks / node["node_id"]).exists()
            ):
                return "cluster_storage_not_shared"
        return None

    # ------------------------------------------------------------------ the loop

    def _may_try(self, primary: JSON | None, now: float) -> bool:
        waited = now - self.started
        if not self.settings.preferred and primary is None and waited < FIRST_WAIT_S:
            return False
        if primary and primary["handover_to"] and primary["handover_to"] != self.settings.node_id:
            if waited < HANDOVER_WAIT_S or (self._last_try is not None and now - self._last_try < HANDOVER_WAIT_S):
                return False
        return self._last_try is None or now - self._last_try >= RETRY_S

    def tick(
        self, *, describe: Callable[[], JSON] | None = None, on_acquired: Callable[[], None] | None = None
    ) -> None:
        """One step of the background loop of ``serve``: heartbeat, renew or try the lock."""
        state = describe() if describe else {}
        self.heartbeat(release=str(state.get("release", "")), generation=state.get("generation"))
        if not self.settings.candidate:
            return
        now = self._clock()
        if self.is_primary:
            if now - self._last_renew >= RENEW_S:
                self.renew()
            return
        self.problem = self.storage_problem()
        if self.problem:
            return
        if not self._may_try(self.primary(), now):
            return
        self._last_try = now
        if self.try_acquire() and on_acquired is not None:
            on_acquired()


def coordinator_for(config: Any, *, agent_version: str = "") -> Coordinator | None:
    settings = settings_for(config)
    if settings is None:
        return None
    return Coordinator(settings, storage_root=Path(config["storage_root"]), agent_version=agent_version)
