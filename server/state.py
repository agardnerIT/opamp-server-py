import hashlib
import json
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from loguru import logger

from server import crypto, redaction
# #68: effective-config reading lives in one shared module so `server/` and the UI
# cannot drift again. Re-exported here because `from server.state import
# parse_effective_config` is the old import path.
from server.effective_config import parse_effective_config


def utcnow():
    return datetime.now(timezone.utc)


def _stringify_any_value(value: Optional[Dict]) -> Optional[str]:
    """Stringify an OpAMP AnyValue oneof dict (as produced by MessageToDict).

    e.g. {"stringValue": "prod"} -> "prod", {"intValue": "42"} -> "42",
    {"boolValue": true} -> "True". Returns None for absent or non-scalar values.
    """
    if not value:
        return None
    for v in value.values():
        if isinstance(v, (str, int, float, bool)):
            return str(v)
    return None


DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
DB_PATH = DATA_DIR / "opamp.db"


def _parse_ts(s: Optional[str]) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s else utcnow()


def _optional_column(row, name: str):
    """Read a column that may predate a migration (sqlite3.Row raises on miss)."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


class SQLiteAgentStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    instance_uid BLOB,
                    connected_at TEXT,
                    last_heartbeat TEXT,
                    healthy INTEGER,
                    capabilities INTEGER DEFAULT 0,
                    description TEXT,
                    effective_config TEXT,
                    remote_config_status TEXT DEFAULT 'UNSET',
                    package_statuses TEXT,
                    available_components TEXT,
                    health TEXT,
                    compliance TEXT,
                    updated_at TEXT,
                    last_remote_config_hash TEXT,
                    remote_config_error TEXT,
                    last_sequence_num INTEGER,
                    pending_command TEXT,
                    command_delivered_at TEXT
                )
            """)

            cursor = conn.execute("PRAGMA table_info(agents)")
            columns = [row[1] for row in cursor.fetchall()]
            if "compliance" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN compliance TEXT")
            # #81: the spec's MUST is a *hash comparison*, so the hash the agent
            # reports has to be stored, not just its status string. Added by
            # migration so an existing database keeps working.
            if "last_remote_config_hash" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN last_remote_config_hash TEXT")
            if "remote_config_error" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN remote_config_error TEXT")
            # #70: the spec's compression detection compares against the *last*
            # sequence_num, so it has to survive a restart. NULL = no baseline yet.
            if "last_sequence_num" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN last_sequence_num INTEGER")
            # #84: a queued server->agent command must survive a restart, because HTTP
            # has no server push — the command is delivered on the agent's next poll.
            if "pending_command" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN pending_command TEXT")
            if "command_delivered_at" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN command_delivered_at TEXT")
            # #75: why this agent's instance_uid changed. Null in both columns unless
            # the server re-issued a uid because the old one was a duplicate.
            if "uid_reissued_from" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN uid_reissued_from TEXT")
            if "uid_reissue_reason" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN uid_reissue_reason TEXT")

    def load_all(self) -> Dict[str, "AgentState"]:
        agents = {}
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT * FROM agents"):
                agents[row["agent_id"]] = self._row_to_state(row)
        return agents

    def upsert(self, state: "AgentState"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO agents (agent_id, instance_uid, connected_at, last_heartbeat,
                                   healthy, capabilities, description, effective_config,
                                   remote_config_status, package_statuses, available_components,
                                   health, compliance, updated_at, last_remote_config_hash,
                                   remote_config_error, last_sequence_num, pending_command,
                                   command_delivered_at, uid_reissued_from,
                                   uid_reissue_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    last_heartbeat=excluded.last_heartbeat,
                    healthy=excluded.healthy,
                    capabilities=excluded.capabilities,
                    description=excluded.description,
                    effective_config=excluded.effective_config,
                    remote_config_status=excluded.remote_config_status,
                    package_statuses=excluded.package_statuses,
                    available_components=excluded.available_components,
                    health=excluded.health,
                    compliance=excluded.compliance,
                    updated_at=excluded.updated_at,
                    last_remote_config_hash=excluded.last_remote_config_hash,
                    remote_config_error=excluded.remote_config_error,
                    last_sequence_num=excluded.last_sequence_num,
                    pending_command=excluded.pending_command,
                    command_delivered_at=excluded.command_delivered_at,
                    uid_reissued_from=excluded.uid_reissued_from,
                    uid_reissue_reason=excluded.uid_reissue_reason
                WHERE excluded.updated_at > agents.updated_at
            """, self._state_to_row(state))

    def remove(self, agent_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))

    def _state_to_row(self, state: "AgentState") -> tuple:
        return (
            state.agent_id,
            state.instance_uid,
            state.connected_at.isoformat(),
            state.last_heartbeat.isoformat(),
            int(state.healthy) if state.healthy is not None else None,
            state.capabilities,
            json.dumps(state.description),
            self._sealed_effective_config(state),
            state.remote_config_status,
            json.dumps(state.package_statuses),
            json.dumps(state.available_components),
            json.dumps(state.health),
            json.dumps(state.compliance) if state.compliance else None,
            utcnow().isoformat(),
            state.last_remote_config_hash,
            state.remote_config_error,
            state.last_sequence_num,
            json.dumps(state.pending_command) if state.pending_command else None,
            state.command_delivered_at,
            state.uid_reissued_from,
            state.uid_reissue_reason,
        )

    @staticmethod
    def _sealed_effective_config(state: "AgentState") -> Optional[str]:
        """Seal the effective config for storage (#98).

        A row that failed to decrypt is written back exactly as it was: an
        unreadable-with-this-key body must not be replaced by NULL just because
        this process could not open it, or supplying the right key later would
        find nothing left to open.
        """
        if state.effective_config is None and state.sealed_effective_config:
            return state.sealed_effective_config
        return crypto.seal_if_configured(state.effective_config, f"agent:{state.agent_id}")

    def _row_to_state(self, row) -> "AgentState":
        sealed = row["effective_config"]
        effective_config = sealed
        config_error = None
        if crypto.is_sealed(sealed):
            try:
                effective_config = crypto.open_sealed(sealed, f"agent:{row['agent_id']}")
            except crypto.ConfigDecryptError as exc:
                # Fail closed per row, not per fleet: one undecryptable config must
                # not make `GET /agents` a 500, and it must not be silently dropped.
                logger.warning(f"agent {row['agent_id']}: {exc}")
                effective_config = None
                config_error = str(exc)
        return AgentState(
            instance_uid=row["instance_uid"],
            agent_id=row["agent_id"],
            connected_at=_parse_ts(row["connected_at"]),
            last_heartbeat=_parse_ts(row["last_heartbeat"]),
            healthy=bool(row["healthy"]) if row["healthy"] is not None else None,
            capabilities=row["capabilities"] or 0,
            description=json.loads(row["description"] or "{}"),
            effective_config=effective_config,
            remote_config_status=row["remote_config_status"] or "UNSET",
            package_statuses=json.loads(row["package_statuses"] or "{}"),
            available_components=json.loads(row["available_components"] or "{}"),
            health=json.loads(row["health"] or "{}"),
            compliance=json.loads(row["compliance"] or "null"),
            last_remote_config_hash=_optional_column(row, "last_remote_config_hash"),
            remote_config_error=_optional_column(row, "remote_config_error"),
            last_sequence_num=_optional_column(row, "last_sequence_num"),
            pending_command=(
                json.loads(_optional_column(row, "pending_command") or "null")
            ),
            command_delivered_at=_optional_column(row, "command_delivered_at"),
            uid_reissued_from=_optional_column(row, "uid_reissued_from"),
            uid_reissue_reason=_optional_column(row, "uid_reissue_reason"),
            sealed_effective_config=sealed if config_error else None,
            config_error=config_error,
        )


class SQLiteMetricsStore:
    """Latest OTLP metric snapshot per agent, persisted to SQLite.

    Single row per agent (no history); all operations are best-effort so the
    server keeps working in-memory if the DB is unavailable.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS agent_metrics ("
                    "agent_id TEXT PRIMARY KEY, metrics TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
        except sqlite3.Error as exc:
            logger.bind(event="store.write_failed", operation="metrics_init", error=str(exc)).warning(
                f"Could not init metrics store at {db_path}: {exc}"
            )

    def upsert(self, agent_id: str, entry: Dict[str, Any]) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO agent_metrics (agent_id, metrics, updated_at) "
                    "VALUES (?, ?, ?)",
                    (agent_id, json.dumps(entry.get("metrics", {})), entry.get("updated_at") or utcnow().isoformat()),
                )
        except sqlite3.Error as exc:
            logger.bind(
                event="store.write_failed",
                operation="metrics_upsert",
                agent_id=agent_id,
                error=str(exc),
            ).warning(f"Could not persist metrics for {agent_id}: {exc}")

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT metrics, updated_at FROM agent_metrics WHERE agent_id=?", (agent_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.bind(
                event="store.read_failed", operation="metrics_get", agent_id=agent_id, error=str(exc)
            ).warning(f"Could not read metrics for {agent_id}: {exc}")
            return None
        if not row:
            return None
        try:
            return {"metrics": json.loads(row[0]), "updated_at": row[1]}
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(f"Corrupt persisted metrics for {agent_id}: {exc}")
            return None

    def delete(self, agent_id: str) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM agent_metrics WHERE agent_id=?", (agent_id,))
        except sqlite3.Error as exc:
            logger.bind(
                event="store.write_failed",
                operation="metrics_delete",
                agent_id=agent_id,
                error=str(exc),
            ).warning(f"Could not delete metrics for {agent_id}: {exc}")

class SQLiteCredentialStore:
    """Per-agent OpAMP credentials (#94): one row per issued token.

    Only the SHA-256 of a token is stored — the token itself exists in the API
    response that created it and nowhere else. Rows are never deleted, so
    `revoked_at`/`last_seen_at` stay available as the audit trail an operator
    needs to watch a fleet finish a cutover.

    Rotation uses `retire_on_use`: the outgoing row is retired automatically the
    first time its replacement authenticates, which is what makes rotation
    zero-downtime without a timer (a timer would either cut too early, breaking a
    collector that has not reloaded yet, or leave the old token alive forever).

    Timestamps are ISO-8601 UTC strings, like every other table here.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_credentials (
                    token_hash TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    revoked_at TEXT,
                    retire_on_use TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS agent_credentials_agent_idx "
                "ON agent_credentials(agent_id)"
            )
            # WAL is a database-level setting, so it also benefits the agent and
            # metrics writes sharing this file, and keeps an offline reader
            # (`opampctl --db`) from blocking the auth path.
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        # A short timeout + busy timeout instead of the default 5s exception:
        # auth runs on every agent message, so a lock held by a metrics write
        # must wait, not fail the request.
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def add(
        self,
        token_hash: str,
        agent_id: str,
        label: str = "",
        retire_on_use: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_credentials "
                "(token_hash, agent_id, label, created_at, retire_on_use) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash, agent_id, label, utcnow().isoformat(), retire_on_use),
            )

    def get(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_credentials WHERE token_hash=?", (token_hash,)
            ).fetchone()
        return self._row(row) if row else None

    def touch(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_credentials SET last_seen_at=? WHERE token_hash=?",
                (utcnow().isoformat(), token_hash),
            )

    def set_retire_on_use(self, token_hash: str, replacement: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_credentials SET retire_on_use=? WHERE token_hash=?",
                (replacement, token_hash),
            )

    def supersede(self, agent_id: str, replacement: str) -> int:
        """Retire every credential that this replacement was rotated in for."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_credentials SET revoked_at=? "
                "WHERE agent_id=? AND retire_on_use=? AND revoked_at IS NULL",
                (utcnow().isoformat(), agent_id, replacement),
            )
            return cursor.rowcount or 0

    def revoke(self, token_hash: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_credentials SET revoked_at=? "
                "WHERE token_hash=? AND revoked_at IS NULL",
                (utcnow().isoformat(), token_hash),
            )
            return bool(cursor.rowcount)

    def revoke_agent(self, agent_id: str) -> int:
        """Revoke every live credential for one agent. Rows are kept."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_credentials SET revoked_at=? "
                "WHERE agent_id=? AND revoked_at IS NULL",
                (utcnow().isoformat(), agent_id),
            )
            return cursor.rowcount or 0

    def active(self, agent_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_credentials "
                "WHERE agent_id=? AND revoked_at IS NULL ORDER BY created_at",
                (agent_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def history(self, agent_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_credentials WHERE agent_id=? ORDER BY created_at",
                (agent_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def all_history(self, agent_ids: Optional[List[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Every credential row grouped by agent, optionally restricted to `agent_ids`."""
        if agent_ids is not None and not agent_ids:
            return {}
        sql = "SELECT * FROM agent_credentials"
        params: tuple = ()
        if agent_ids is not None:
            placeholders = ",".join("?" for _ in agent_ids)
            sql += f" WHERE agent_id IN ({placeholders})"
            params = tuple(agent_ids)
        sql += " ORDER BY created_at"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row["agent_id"]].append(self._row(row))
        return dict(grouped)


class SQLitePrincipalStore:
    """Admin identities and their roles (#95): one row per human/automation principal.

    `secret_hash` is always a slow hash (`server.auth.hash_password`, scrypt) —
    unlike an agent token, a human password has little entropy, so a fast hash
    would turn one database copy into a wordlist run.

    Rows are never deleted: revocation sets `revoked_at`, which keeps the name
    reserved (a revoked `alice` cannot be silently re-created by someone else)
    and keeps the audit trail intelligible after the fact.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_principals (
                    principal_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT,
                    last_seen_at TEXT
                )
            """)
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def create(
        self,
        principal_id: str,
        name: str,
        role: str,
        secret_hash: str,
        expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO admin_principals "
                    "(principal_id, name, role, secret_hash, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        principal_id,
                        name,
                        role,
                        secret_hash,
                        utcnow().isoformat(),
                        expires_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"principal name {name!r} is already taken") from exc
        return self.get(principal_id) or {}

    def get(self, principal_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM admin_principals WHERE principal_id=?", (principal_id,)
            ).fetchone()
        return self._row(row) if row else None

    def by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM admin_principals WHERE name=?", (name,)
            ).fetchone()
        return self._row(row) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM admin_principals ORDER BY created_at"
            ).fetchall()
        return [self._row(row) for row in rows]

    def revoke(self, principal_id: str) -> bool:
        """Revoke a principal. Returns False if it was already revoked/absent."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE admin_principals SET revoked_at=? "
                "WHERE principal_id=? AND revoked_at IS NULL",
                (utcnow().isoformat(), principal_id),
            )
            return bool(cursor.rowcount)

    def touch(self, principal_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE admin_principals SET last_seen_at=? WHERE principal_id=?",
                (utcnow().isoformat(), principal_id),
            )

    def count_live(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM admin_principals WHERE revoked_at IS NULL"
            ).fetchone()
        return int(row["n"]) if row else 0


class SQLiteSessionStore:
    """Admin sessions (#95): opaque bearer tokens, SHA-256 at rest.

    Sessions live in SQLite rather than process memory because uvicorn runs
    multiple workers: an in-memory session works only if the load balancer is
    sticky, which turns "add a replica" into "log everyone out at random".

    Both `expires_at` (absolute TTL) and `last_seen_at` (idle timeout) are checked
    on every use; `revoked_at` makes logout and "revoke that device" the same
    operation.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    session_hash TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    client_ip TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS admin_sessions_principal_idx "
                "ON admin_sessions(principal_id)"
            )
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def create(
        self,
        session_hash: str,
        principal_id: str,
        expires_at: str,
        client_ip: str = "",
        user_agent: str = "",
    ) -> Dict[str, Any]:
        now = utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO admin_sessions "
                "(session_hash, principal_id, created_at, last_seen_at, expires_at, "
                "client_ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_hash,
                    principal_id,
                    now,
                    now,
                    expires_at,
                    client_ip[:64],
                    user_agent[:256],
                ),
            )
        return self.get(session_hash) or {}

    def get(self, session_hash: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM admin_sessions WHERE session_hash=?", (session_hash,)
            ).fetchone()
        return self._row(row) if row else None

    def touch(self, session_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE admin_sessions SET last_seen_at=? WHERE session_hash=?",
                (utcnow().isoformat(), session_hash),
            )

    def revoke(self, session_hash: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE admin_sessions SET revoked_at=? "
                "WHERE session_hash=? AND revoked_at IS NULL",
                (utcnow().isoformat(), session_hash),
            )
            return bool(cursor.rowcount)

    def revoke_principal(self, principal_id: str) -> int:
        """Revoke every live session for a principal (called when it is revoked)."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE admin_sessions SET revoked_at=? "
                "WHERE principal_id=? AND revoked_at IS NULL",
                (utcnow().isoformat(), principal_id),
            )
            return cursor.rowcount or 0

    def list_all(self) -> List[Dict[str, Any]]:
        """Every live session, regardless of principal (admin listing)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM admin_sessions WHERE revoked_at IS NULL "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_for_principal(self, principal_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM admin_sessions WHERE principal_id=? "
                "AND revoked_at IS NULL ORDER BY created_at DESC",
                (principal_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def delete_expired(self, now: Optional[str] = None) -> int:
        """Drop sessions whose absolute TTL has passed. Returns how many went."""
        cutoff = now or utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at < ?", (cutoff,)
            )
            return cursor.rowcount or 0


# ------------------------------------------------------------------- remote config


class SQLiteDesiredConfigStore:
    """Desired (remote) collector config per agent (#81) with its lifecycle (#99).

    Two tables, on purpose:

    ``config_revisions``
        Append-only. A revision is immutable (epic #93, D4) and is what the audit
        log points at, what a rollout refers to, and what retention prunes. The
        body is sealed with `server.crypto` under AAD ``revision:<id>`` (#98), so
        copying a blob into another row fails to decrypt rather than handing an
        agent someone else's config. ``status`` is the lifecycle:
        ``proposed → active → superseded | rolled_back | poisoned``, or
        ``rejected`` from ``proposed``.

    ``desired_configs``
        One row per agent and **this is #99's `config_assignments`**: *which*
        revision the agent should run plus the delivery facts (`delivered_hash`,
        `delivered_at`, `last_status`, `attempts`, `poisoned`). Keeping the pointer
        and the delivery facts in one row is what makes "what should it run?" and
        "did it get it?" impossible to disagree. ``previous_revision_id`` is kept
        so a rollback is a pointer move, not a re-upload — rollback still creates a
        new revision, because revisions are immutable.

    Revisions are never updated in place except by retention (which nulls the body
    and keeps hash + metadata) and by lifecycle transitions (a status fact, not
    content). That is what makes "what did we push, when, to whom, and what did it
    do" answerable.
    """

    # Lifecycle statuses. Only `active` is deliverable, and only while it is not
    # poisoned (epic #93, D6: never auto-retry a failure).
    STATUS_PROPOSED = "proposed"
    STATUS_ACTIVE = "active"
    STATUS_SUPERSEDED = "superseded"
    STATUS_ROLLED_BACK = "rolled_back"
    STATUS_REJECTED = "rejected"
    STATUS_POISONED = "poisoned"
    STATUSES = (
        STATUS_PROPOSED,
        STATUS_ACTIVE,
        STATUS_SUPERSEDED,
        STATUS_ROLLED_BACK,
        STATUS_REJECTED,
        STATUS_POISONED,
    )
    DELIVERABLE_STATUSES = (STATUS_ACTIVE,)

    # Columns added after the first release. `ALTER TABLE ... ADD COLUMN` is the
    # migration: SQLite cannot add a NOT NULL column without a default, so every
    # one of these carries one (or is nullable).
    _REVISION_COLUMNS = {
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "proposal_id": "TEXT",
        "scope_kind": "TEXT",
        "scope_json": "TEXT",
        "note": "TEXT",
        "approved_by": "TEXT",
        "approved_at": "TEXT",
        "parent_revision_id": "TEXT",
        "verdict_json": "TEXT",
        "superseded_at": "TEXT",
    }
    _ASSIGNMENT_COLUMNS = {
        "delivered_hash": "TEXT",
        "delivered_at": "TEXT",
        # #102: how the config arrived — `push` (unsolicited WS frame) or `reply`
        # (carried on the agent's own request). NULL = queued, never delivered.
        "delivery_channel": "TEXT",
        "last_status": "TEXT",
        "last_error": "TEXT",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "poisoned": "INTEGER NOT NULL DEFAULT 0",
        "unconfirmed": "INTEGER NOT NULL DEFAULT 0",
        # #101: a staged rollout assigns every revision up front but *holds* delivery
        # until that agent's batch is promoted. Held is a property of this
        # assignment (it resets on the next one), so a rollout that is aborted or
        # expires simply leaves it in place: the agent is never handed a config the
        # gate has not cleared, and any new revision releases it.
        "held": "INTEGER NOT NULL DEFAULT 0",
    }
    # Delivery facts joined onto the current revision. One query per agent gives the
    # message path everything it needs without a second lookup (#109).
    _ASSIGNMENT_SELECT = ", ".join(
        f"d.{column} AS {column}" for column in _ASSIGNMENT_COLUMNS
    )

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS config_revisions (
                    revision_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    config_blob TEXT,
                    config_hash TEXT NOT NULL,
                    author TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    withdrawn INTEGER NOT NULL DEFAULT 0,
                    failed_at TEXT,
                    failure_detail TEXT,
                    body_pruned_at TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS config_revisions_agent_idx "
                "ON config_revisions(agent_id, created_at)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS desired_configs (
                    agent_id TEXT PRIMARY KEY,
                    revision_id TEXT NOT NULL,
                    previous_revision_id TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            self._migrate(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS config_revisions_proposal_idx "
                "ON config_revisions(proposal_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS config_revisions_status_idx "
                "ON config_revisions(status, created_at)"
            )
            conn.execute("PRAGMA journal_mode=WAL")

    @classmethod
    def _migrate(cls, conn: sqlite3.Connection) -> None:
        """Add the #99 columns to a database created by an earlier release."""
        for table, columns in (
            ("config_revisions", cls._REVISION_COLUMNS),
            ("desired_configs", cls._ASSIGNMENT_COLUMNS),
        ):
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, declaration in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        record = {key: row[key] for key in row.keys()}
        for flag in ("withdrawn", "poisoned", "unconfirmed", "held"):
            if flag in record:
                record[flag] = bool(record[flag])
        if "attempts" in record:
            record["attempts"] = int(record["attempts"] or 0)
        return record

    # ------------------------------------------------------------------ writes

    def put(
        self,
        agent_id: str,
        revision_id: str,
        config_map: Dict[str, Any],
        config_hash: str,
        author: str,
        withdrawn: bool = False,
        *,
        status: str = STATUS_ACTIVE,
        proposal_id: Optional[str] = None,
        scope_kind: Optional[str] = None,
        scope_json: Optional[str] = None,
        note: Optional[str] = None,
        parent_revision_id: Optional[str] = None,
        verdict_json: Optional[str] = None,
        make_current: bool = True,
        held: bool = False,
    ) -> Dict[str, Any]:
        """Append a revision, optionally making it the desired state for `agent_id`.

        `make_current=False` is the propose step (#99): the revision exists, is
        auditable and validated, but no agent is pointed at it until an approver
        assigns it. `config_map` is stored sealed; `config_hash` is stored in the
        clear because it is not a secret (it is the value the agent echoes back and
        the value the audit log records).
        """
        if status not in self.STATUSES:
            raise ValueError(f"unknown revision status {status!r}")
        blob = crypto.seal_if_configured(
            json.dumps({"config_map": config_map}, sort_keys=True),
            f"revision:{revision_id}",
        )
        now = utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO config_revisions (revision_id, agent_id, config_blob, "
                "config_hash, author, created_at, withdrawn, status, proposal_id, "
                "scope_kind, scope_json, note, parent_revision_id, verdict_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    agent_id,
                    blob,
                    config_hash,
                    author,
                    now,
                    int(withdrawn),
                    status,
                    proposal_id,
                    scope_kind,
                    scope_json,
                    note,
                    parent_revision_id,
                    verdict_json,
                ),
            )
            if make_current:
                self._assign_on(conn, agent_id, revision_id, now, held=held)
        # `current()` for a made-current revision (it carries the delivery facts);
        # the new row itself when the caller is only proposing.
        if make_current:
            return self.current(agent_id) or self.revision(revision_id) or {}
        return self.revision(revision_id) or {}

    def _assign_on(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        revision_id: str,
        now: Optional[str] = None,
        held: bool = False,
    ) -> None:
        """Point `agent_id` at `revision_id`, resetting the delivery facts.

        `previous_revision_id` is the rollback target, so it only moves when the
        assignment actually changes. The delivery counters reset because they
        describe *this* assignment, not the agent.
        """
        now = now or utcnow().isoformat()
        previous = conn.execute(
            "SELECT revision_id FROM desired_configs WHERE agent_id=?", (agent_id,)
        ).fetchone()
        previous_id = previous["revision_id"] if previous else None
        if previous_id and previous_id != revision_id:
            # #99 rule 3, enforced here so *every* path keeps it (direct push,
            # withdrawal, fleet apply, approve): one active revision per agent. A
            # poisoned or rolled-back revision keeps its status — only `active` is
            # replaced, so the reason the old one is gone is never overwritten.
            conn.execute(
                "UPDATE config_revisions SET status='superseded', superseded_at=? "
                "WHERE revision_id=? AND status='active'",
                (now, previous_id),
            )
        conn.execute(
            "INSERT INTO desired_configs "
            "(agent_id, revision_id, previous_revision_id, updated_at, held) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "revision_id=excluded.revision_id, "
            "previous_revision_id=CASE "
            "  WHEN desired_configs.revision_id = excluded.revision_id "
            "  THEN desired_configs.previous_revision_id "
            "  ELSE desired_configs.revision_id END, "
            "delivered_hash=NULL, delivered_at=NULL, delivery_channel=NULL, "
            "last_status=NULL, last_error=NULL, "
            # Poison is a fact about the revision that failed (D6), not about the
            # agent: a new assignment must be deliverable even when the one it
            # replaces was poisoned. Without this a rollback after a failed push is
            # silently undeliverable — the one-click recovery the epic promises.
            "attempts=0, poisoned=0, unconfirmed=0, held=excluded.held, "
            "updated_at=excluded.updated_at",
            (agent_id, revision_id, previous_id, now, int(held)),
        )

    def assign(self, agent_id: str, revision_id: str, held: bool = False) -> None:
        """Approve-time assignment: make `revision_id` the agent's desired config."""
        with self._connect() as conn:
            self._assign_on(conn, agent_id, revision_id, held=held)

    def set_held(self, agent_id: str, held: bool) -> bool:
        """Release or re-apply the rollout hold on an agent's assignment (#101).

        Returns False when the agent has no assignment (nothing to hold), so a
        rollout that has been overtaken by a newer revision cannot resurrect it.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE desired_configs SET held=?, updated_at=? WHERE agent_id=?",
                (int(held), utcnow().isoformat(), agent_id),
            )
            return bool(cursor.rowcount)

    def set_status(
        self,
        revision_id: str,
        status: str,
        **fields: Any,
    ) -> None:
        """One lifecycle transition on a revision (content is never touched).

        `fields` must be real columns; the allowed set is fixed so a caller cannot
        write `config_blob` here — immutability (D4) is enforced by the shape of
        this method, not by discipline.
        """
        if status not in self.STATUSES:
            raise ValueError(f"unknown revision status {status!r}")
        allowed = {
            "approved_by",
            "approved_at",
            "superseded_at",
            "failed_at",
            "failure_detail",
            "verdict_json",
            "note",
            "scope_json",
        }
        assignments = ["status=?"]
        values: List[Any] = [status]
        for name, value in fields.items():
            if name not in allowed:
                raise ValueError(f"cannot set {name!r} on a revision")
            assignments.append(f"{name}=?")
            values.append(value)
        values.append(revision_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE config_revisions SET {', '.join(assignments)} WHERE revision_id=?",
                tuple(values),
            )

    def poison_revision(self, revision_id: str, detail: str = "") -> None:
        """Mark a revision permanently undeliverable (D6).

        Poisoning is a fact about that revision id, not a mood: retrying means a new
        revision, which is the human decision the epic requires.
        """
        now = utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE config_revisions SET status=?, failed_at=COALESCE(failed_at, ?), "
                "failure_detail=COALESCE(failure_detail, ?) WHERE revision_id=?",
                (self.STATUS_POISONED, now, detail[:2000], revision_id),
            )
            conn.execute(
                "UPDATE desired_configs SET poisoned=1, last_error=?, updated_at=? "
                "WHERE revision_id=?",
                (detail[:2000], now, revision_id),
            )

    def clear_poison(self, revision_id: str) -> None:
        """Reactivation after explicit approver re-approval (#99 rule 5)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE config_revisions SET status=?, failed_at=NULL, failure_detail=NULL "
                "WHERE revision_id=?",
                (self.STATUS_ACTIVE, revision_id),
            )
            conn.execute(
                "UPDATE desired_configs SET poisoned=0, last_error=NULL, attempts=0 "
                "WHERE revision_id=?",
                (revision_id,),
            )

    def record_delivery(
        self, agent_id: str, revision_id: str, channel: Optional[str] = None
    ) -> int:
        """Count one delivery of the current revision; returns the new attempt count.

        Called once per frame we put on the wire, so an agent that never reports back
        cannot be re-pushed forever (`CONFIG_MAX_DELIVERY_ATTEMPTS`). `channel` (#102)
        records *how* it got there, because "delivered by a push" and "delivered on
        the next poll" are different promises to an operator.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE desired_configs SET delivered_hash=("
                "  SELECT config_hash FROM config_revisions WHERE revision_id=?"
                "), delivered_at=?, delivery_channel=?, attempts=attempts+1, updated_at=? "
                "WHERE agent_id=? AND revision_id=?",
                (
                    revision_id,
                    utcnow().isoformat(),
                    channel,
                    utcnow().isoformat(),
                    agent_id,
                    revision_id,
                ),
            )
            row = conn.execute(
                "SELECT attempts FROM desired_configs WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return int(row["attempts"] or 0) if row else 0

    def note_report(
        self,
        agent_id: str,
        status: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        """Record what the agent says about the config we assigned it (no content)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE desired_configs SET last_status=?, last_error=?, updated_at=? "
                "WHERE agent_id=?",
                (
                    (status or "")[:40] or None,
                    (error or "")[:2000] or None,
                    utcnow().isoformat(),
                    agent_id,
                ),
            )

    def mark_unconfirmed(self, agent_id: str, detail: str = "") -> None:
        """Stop delivering: the agent never acknowledged this revision.

        Not poisoning — the revision may be fine and the agent wedged. The operator
        sees `unconfirmed: true`, and a new revision (or a reactivation) clears it.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE desired_configs SET unconfirmed=1, last_error=?, updated_at=? "
                "WHERE agent_id=?",
                (detail[:2000], utcnow().isoformat(), agent_id),
            )

    def clear(
        self, agent_id: str) -> None:
        """Forget the desired state (used by tests and by a hard withdrawal)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM desired_configs WHERE agent_id=?", (agent_id,))

    # ------------------------------------------------------------------- reads

    def current(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """The agent's assignment: current revision **plus** the delivery facts."""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT r.*, {self._ASSIGNMENT_SELECT} FROM desired_configs d "
                "JOIN config_revisions r ON r.revision_id = d.revision_id "
                "WHERE d.agent_id=?",
                (agent_id,),
            ).fetchone()
        return self._row(row) if row else None

    def previous(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT r.* FROM desired_configs d "
                "JOIN config_revisions r ON r.revision_id = d.previous_revision_id "
                "WHERE d.agent_id=?",
                (agent_id,),
            ).fetchone()
        return self._row(row) if row else None

    def revision(self, revision_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM config_revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
        return self._row(row) if row else None

    def history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM config_revisions WHERE agent_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_revisions(
        self,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        proposal_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Revision metadata for the admin surfaces (#99 item 5), newest first."""
        where: List[str] = []
        values: List[Any] = []
        if agent_id:
            where.append("agent_id=?")
            values.append(agent_id)
        if status:
            where.append("status=?")
            values.append(status)
        if proposal_id:
            where.append("proposal_id=?")
            values.append(proposal_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        values.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM config_revisions" + clause +
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [self._row(row) for row in rows]

    def by_proposal(self, proposal_id: str) -> List[Dict[str, Any]]:
        """Every per-agent revision created by one propose/approve call (#99)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM config_revisions WHERE proposal_id=? "
                "ORDER BY created_at, rowid",
                (proposal_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def hashes_for_agent(self, agent_id: str) -> List[str]:
        """Hash of every revision this agent has ever been assigned (T9 detection)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT config_hash FROM config_revisions WHERE agent_id=?",
                (agent_id,),
            ).fetchall()
        return [row["config_hash"] for row in rows]

    def load_current(self) -> Dict[str, Dict[str, Any]]:
        """Every agent's assignment, for the in-memory cache."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT r.*, {self._ASSIGNMENT_SELECT} FROM desired_configs d "
                "JOIN config_revisions r ON r.revision_id = d.revision_id"
            ).fetchall()
        return {row["agent_id"]: self._row(row) for row in rows}

    def mark_failed(self, revision_id: str, detail: str = "") -> None:
        """Backwards-compatible alias for `poison_revision` (#81 call sites)."""
        self.poison_revision(revision_id, detail)

    def sealed_revisions(self) -> List[Dict[str, Any]]:
        """Sealed bodies still present — the rotation job's work list (#98)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT revision_id, config_blob FROM config_revisions "
                "WHERE config_blob IS NOT NULL"
            ).fetchall()
        return [self._row(row) for row in rows]

    def reseal(self, revision_id: str, blob: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE config_revisions SET config_blob=? WHERE revision_id=?",
                (blob, revision_id),
            )

    def prune(self, cutoff: str) -> int:
        """Drop bodies of superseded revisions older than `cutoff` (#98 retention).

        Hash, author and timestamps stay: that is the audit trail. The *current*
        revision is never pruned regardless of age — a desired config that is still
        being delivered must stay readable, or a restart would forget the fleet.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE config_revisions SET config_blob=NULL, body_pruned_at=? "
                "WHERE created_at < ? AND config_blob IS NOT NULL "
                "AND revision_id NOT IN (SELECT revision_id FROM desired_configs)",
                (utcnow().isoformat(), cutoff),
            )
            return cursor.rowcount or 0


class SQLiteRolloutStore:
    """Staged rollouts (#101): one row per rollout, one row per (rollout, agent).

    Two tables, because they answer two different questions and are written at two
    different rates: `config_rollouts` is the operator-visible state machine (a
    handful of rows, one update per batch transition) and `config_rollout_agents` is
    the blast-radius record (one row per agent, batched reads on the tick).

    The batch plan is **stored, not recomputed** (`batch_index` per agent plus the
    settings that produced it). A rollout whose plan changed between ticks because the
    fleet changed underneath it would be a second, undocumented rollout.
    """

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_PAUSED = "paused"
    STATUS_COMPLETED = "completed"
    STATUS_ABORTED = "aborted"
    STATUS_CANCELLED = "cancelled"
    STATUSES = (
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_PAUSED,
        STATUS_COMPLETED,
        STATUS_ABORTED,
        STATUS_CANCELLED,
    )
    # Statuses that still control delivery (and therefore still expire on TTL).
    OPEN_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_PAUSED)

    # Per-agent state vocabulary, from the issue: pending -> delivered -> applying ->
    # applied | failed | reverted | paused. `applying` is the agent's own word.
    AGENT_STATES = (
        "pending",
        "delivered",
        "applying",
        "applied",
        "failed",
        "reverted",
        "paused",
    )

    # Columns added after the first release (ALTER TABLE is the migration).
    _ROLLOUT_COLUMNS = {
        # #84: "push a config then restart it" is one operator action, so a rollout
        # can queue a restart command for each batch once its gate opens.
        "restart_after_apply": "INTEGER NOT NULL DEFAULT 0",
    }

    # Editable rollout columns. Fixed set: a caller cannot rewrite `created_at` or the
    # recorded batch plan, which is what makes a rollout reproducible in tests.
    _ROLLOUT_FIELDS = {
        "status",
        "current_batch",
        "batch_started_at",
        "paused_reason",
        "pause_detail",
        "paused_at",
        "completed_at",
        "cancelled_at",
        "detail_json",
        "updated_at",
    }
    _AGENT_FIELDS = {"state", "delivered_at", "applied_at", "detail", "updated_at"}

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS config_rollouts (
                    rollout_id TEXT PRIMARY KEY,
                    proposal_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'promote',
                    status TEXT NOT NULL,
                    config_hash TEXT,
                    author TEXT,
                    approver TEXT,
                    scope_json TEXT,
                    note TEXT,
                    canary_percent REAL,
                    max_agents_per_batch INTEGER,
                    max_percent_per_batch INTEGER,
                    health_gate_seconds INTEGER,
                    ttl_minutes INTEGER,
                    batch_size INTEGER,
                    total_agents INTEGER,
                    batch_count INTEGER,
                    current_batch INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    batch_started_at TEXT,
                    paused_reason TEXT,
                    pause_detail TEXT,
                    paused_at TEXT,
                    completed_at TEXT,
                    cancelled_at TEXT,
                    detail_json TEXT,
                    restart_after_apply INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS config_rollout_agents (
                    rollout_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    batch_index INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    revision_id TEXT,
                    config_hash TEXT,
                    parent_revision_id TEXT,
                    delivered_at TEXT,
                    applied_at TEXT,
                    detail TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (rollout_id, agent_id)
                )
            """)
            # Columns added after #101's first release (the rollout table is the
            # youngest one in the schema, so it gets the same in-place migration the
            # older tables use rather than a version table).
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(config_rollouts)").fetchall()
            }
            for name, declaration in self._ROLLOUT_COLUMNS.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE config_rollouts ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS config_rollouts_status_idx "
                "ON config_rollouts(status, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS config_rollout_agents_batch_idx "
                "ON config_rollout_agents(rollout_id, batch_index)"
            )
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        record = {key: row[key] for key in row.keys()}
        for numeric in ("current_batch", "batch_count", "total_agents", "batch_size"):
            if record.get(numeric) is not None:
                record[numeric] = int(record[numeric])
        return record

    # ------------------------------------------------------------------ writes

    def create(self, rollout_id: str, **fields: Any) -> Dict[str, Any]:
        """Insert one rollout row."""
        values = dict(fields)
        now = utcnow().isoformat()
        values.setdefault("created_at", now)
        values.setdefault("updated_at", now)
        values["rollout_id"] = rollout_id
        columns = list(values)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO config_rollouts ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        return self.get(rollout_id) or {}

    def update(self, rollout_id: str, **fields: Any) -> None:
        """One state transition. The editable set is fixed on purpose."""
        assignments = []
        values: List[Any] = []
        for name, value in fields.items():
            if name not in self._ROLLOUT_FIELDS:
                raise ValueError(f"cannot set {name!r} on a rollout")
            assignments.append(f"{name}=?")
            values.append(value)
        if not assignments:
            return
        assignments.append("updated_at=?")
        values.append(utcnow().isoformat())
        values.append(rollout_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE config_rollouts SET {', '.join(assignments)} WHERE rollout_id=?",
                tuple(values),
            )

    def put_agent(self, rollout_id: str, agent_id: str, **fields: Any) -> None:
        """Record one agent's place in the plan (written once, at creation)."""
        values = dict(fields)
        values.setdefault("state", "pending")
        values.setdefault("updated_at", utcnow().isoformat())
        values["rollout_id"] = rollout_id
        values["agent_id"] = agent_id
        columns = list(values)
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO config_rollout_agents ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )

    def set_agent(self, rollout_id: str, agent_id: str, **fields: Any) -> None:
        assignments = []
        values: List[Any] = []
        for name, value in fields.items():
            if name not in self._AGENT_FIELDS:
                raise ValueError(f"cannot set {name!r} on a rollout agent")
            assignments.append(f"{name}=?")
            values.append(value)
        if not assignments:
            return
        assignments.append("updated_at=?")
        values.append(utcnow().isoformat())
        values.extend([rollout_id, agent_id])
        with self._connect() as conn:
            conn.execute(
                "UPDATE config_rollout_agents SET "
                + ", ".join(assignments)
                + " WHERE rollout_id=? AND agent_id=?",
                tuple(values),
            )

    # ------------------------------------------------------------------- reads

    def get(self, rollout_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM config_rollouts WHERE rollout_id=?", (rollout_id,)
            ).fetchone()
        return self._row(row) if row else None

    def list(
        self,
        statuses: Optional[List[str]] = None,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        values: List[Any] = []
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            values.extend(statuses)
        if kind:
            where.append("kind=?")
            values.append(kind)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        values.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM config_rollouts" + clause
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [self._row(row) for row in rows]

    def agents(self, rollout_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM config_rollout_agents WHERE rollout_id=? "
                "ORDER BY batch_index, agent_id",
                (rollout_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def state_counts(self, rollout_ids: List[str]) -> Dict[str, int]:
        """`{state: n}` for the agent gauge - one query for the whole tick."""
        if not rollout_ids:
            return {}
        placeholders = ", ".join("?" for _ in rollout_ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM config_rollout_agents "
                f"WHERE rollout_id IN ({placeholders}) GROUP BY state",
                tuple(rollout_ids),
            ).fetchall()
        return {row["state"]: int(row["n"]) for row in rows}


# ---------------------------------------------------------------------------- audit

# The hash that starts the chain: the `prev_hash` of the very first row. Fixed so a
# verifier can tell "nothing was ever deleted" from "something was deleted".
AUDIT_GENESIS = "0" * 64

# Columns covered by `entry_hash`, in a fixed order. `id` is excluded on purpose:
# it is assigned by SQLite, so including it would make the hash unverifiable from
# the stored values alone. `entry_hash` cannot cover itself.
AUDIT_HASH_FIELDS = (
    "ts",
    "actor",
    "actor_kind",
    "role",
    "action",
    "target_kind",
    "target_id",
    "revision_id",
    "config_hash",
    "result",
    "detail_json",
    "prev_hash",
)

AUDIT_COLUMNS = AUDIT_HASH_FIELDS + ("entry_hash",)

# Export order: `id` first (what a human sorts by), then the hashed columns.
AUDIT_EXPORT_COLUMNS = ("id",) + AUDIT_HASH_FIELDS + ("entry_hash",)


def audit_entry_hash(values: Dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of one audit row (sans `id`/`entry_hash`).

    Canonical = sorted keys, no whitespace, UTF-8, `None` rendered as `null`. Any
    change to any covered field changes the hash, which is what makes a silent edit
    detectable by `verify()` without write access to the chain.
    """
    canonical = {field: values.get(field) for field in AUDIT_HASH_FIELDS}
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalise_audit_ts(value: Optional[str]) -> Optional[str]:
    """Re-serialise a caller-supplied timestamp so string comparison is safe.

    Rows are written with `datetime.isoformat()` in UTC (`+00:00`). A filter of
    `2026-10-01T00:00:00Z` is valid for a human and would sort wrongly against
    `+00:00` suffixes, so bounds go through the same round trip as the rows.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class SQLiteAuditStore:
    """Append-only, hash-chained audit log (#97).

    One table, and every row's `entry_hash` covers the row plus the previous row's
    hash. Silent edits are detectable by :meth:`verify`; an attacker with write
    access can rebuild the chain from the start, which is why the docs say export
    entries off-box if you need real assurance.

    Writes are **fail-closed**: `append()` raises so a privileged caller can refuse
    to act. Reads are best-effort in the caller (`server/audit.py`), because a
    dashboard that cannot read the log should show an error, not crash the server.

    `BEGIN IMMEDIATE` around read-tail-then-insert is the part that matters under
    load: uvicorn runs one worker today, but a second one would otherwise read the
    same tail, compute two different rows with the same `prev_hash`, and split the
    chain. The write lock serialises them instead.

    Pruning keeps the chain verifiable: a prune first appends an `audit.checkpoint`
    row recording the hash it cut to, so `verify()` anchors there instead of
    reporting a break that would then be permanent.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    actor_kind TEXT NOT NULL,
                    role TEXT,
                    action TEXT NOT NULL,
                    target_kind TEXT,
                    target_id TEXT,
                    revision_id TEXT,
                    config_hash TEXT,
                    result TEXT NOT NULL,
                    detail_json TEXT,
                    prev_hash TEXT,
                    entry_hash TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log(ts)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS audit_log_actor_idx ON audit_log(actor)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS audit_log_action_idx ON audit_log(action)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS audit_log_target_idx "
                "ON audit_log(target_kind, target_id)"
            )
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    # ------------------------------------------------------------------ writes

    def _append_on(self, conn: sqlite3.Connection, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Insert one row on an already-open write transaction."""
        tail = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        row = {field: entry.get(field) for field in AUDIT_HASH_FIELDS}
        row["prev_hash"] = tail["entry_hash"] if tail else AUDIT_GENESIS
        row["entry_hash"] = audit_entry_hash(row)
        conn.execute(
            "INSERT INTO audit_log ({}) VALUES ({})".format(
                ", ".join(AUDIT_COLUMNS), ", ".join("?" for _ in AUDIT_COLUMNS)
            ),
            tuple(row[column] for column in AUDIT_COLUMNS),
        )
        row["id"] = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        return row

    def append(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Append one entry and return the stored row (including `id`/`entry_hash`).

        Raises `sqlite3.Error` when the row cannot be written — the caller decides
        whether that is fatal (privileged mutations) or not (denials).
        """
        conn = self._connect()
        try:
            # Manual transaction: `sqlite3`'s implicit BEGIN is deferred, and a
            # deferred read followed by a write is how two writers race for the tail.
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._append_on(conn, entry)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return row
        finally:
            conn.close()

    def prune(self, cutoff: str, limit: int = 10000) -> int:
        """Delete entries older than `cutoff`, with a checkpoint row recording the cut.

        Returns the number of rows deleted (0 when nothing is old enough). The
        checkpoint is appended *before* the delete and is itself retained, so the
        first surviving row's `prev_hash` is explainable rather than alarming.
        """
        cutoff = _normalise_audit_ts(cutoff) or cutoff
        conn = self._connect()
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT id, ts, entry_hash FROM audit_log "
                    "WHERE ts < ? AND action != 'audit.checkpoint' "
                    "ORDER BY id LIMIT ?",
                    (cutoff, int(limit)),
                ).fetchall()
                if not rows:
                    conn.execute("COMMIT")
                    return 0
                last = rows[-1]
                self._append_on(
                    conn,
                    {
                        "ts": utcnow().isoformat(),
                        "actor": "system",
                        "actor_kind": "system",
                        "role": None,
                        "action": "audit.checkpoint",
                        "target_kind": "server",
                        "target_id": None,
                        "revision_id": None,
                        "config_hash": None,
                        "result": "ok",
                        "detail_json": json.dumps(
                            {
                                "pruned_rows": len(rows),
                                "pruned_through_id": last["id"],
                                "pruned_through_ts": last["ts"],
                                "pruned_through_hash": last["entry_hash"],
                                "cutoff": cutoff,
                            },
                            sort_keys=True,
                        ),
                    },
                )
                conn.execute("DELETE FROM audit_log WHERE id <= ?", (last["id"],))
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return len(rows)
        finally:
            conn.close()

    # ------------------------------------------------------------------- reads

    def list_entries(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        target: Optional[str] = None,
        result: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Newest first. Filters are exact except `action`, which is a prefix.

        Prefix matching on `action` is what makes `action=config.` a useful filter
        (`config.propose`, `config.apply`, ...) without a second parameter.
        """
        where, params = _audit_filter_clause(since, until, actor, action, target, result)
        sql = "SELECT * FROM audit_log" + where + " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        with self._connect() as conn:
            return [self._row(row) for row in conn.execute(sql, params).fetchall()]

    def count_entries(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        target: Optional[str] = None,
        result: Optional[str] = None,
    ) -> int:
        """Rows matching the same filters as `list_entries` (for pagination)."""
        where, params = _audit_filter_clause(since, until, actor, action, target, result)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log" + where, params
            ).fetchone()
        return int(row["n"]) if row else 0

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
        return int(row["n"]) if row else 0

    def bounds(self) -> Dict[str, Any]:
        """`{count, first_ts, last_ts, first_id, last_id}` for status displays."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
                "MIN(id) AS first_id, MAX(id) AS last_id FROM audit_log"
            ).fetchone()
        return {
            "count": int(row["n"] or 0),
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"],
            "first_id": row["first_id"],
            "last_id": row["last_id"],
        }

    def verify(self) -> Dict[str, Any]:
        """Recompute the whole chain. Returns a verdict, never raises on tampering.

        Reported fields: `ok`, counts, the id where verification broke (if it did),
        why, and whether the chain was anchored by a retention checkpoint (which is
        the one legitimate reason the first retained row does not point at genesis).
        """
        with self._connect() as conn:
            rows = [
                self._row(row)
                for row in conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
            ]

        report: Dict[str, Any] = {
            "ok": True,
            "entries": len(rows),
            "checked": 0,
            "broken_at": None,
            "reason": None,
            "anchored_by_checkpoint": False,
            "first_id": rows[0]["id"] if rows else None,
            "last_id": rows[-1]["id"] if rows else None,
            "first_ts": rows[0]["ts"] if rows else None,
            "last_ts": rows[-1]["ts"] if rows else None,
        }
        if not rows:
            return report

        expected_prev = AUDIT_GENESIS
        first_prev = rows[0]["prev_hash"]
        if first_prev != AUDIT_GENESIS:
            anchor = _checkpoint_anchor(rows, first_prev)
            if anchor is None:
                report.update(
                    ok=False,
                    broken_at=rows[0]["id"],
                    reason=(
                        "the first retained entry does not chain to genesis and no "
                        "checkpoint accounts for the gap: entries were deleted"
                    ),
                )
                return report
            report["anchored_by_checkpoint"] = True
            expected_prev = first_prev

        for row in rows:
            report["checked"] += 1
            if row["prev_hash"] != expected_prev:
                report.update(
                    ok=False,
                    broken_at=row["id"],
                    reason="prev_hash does not match the previous entry: a row was removed or reordered",
                )
                return report
            if audit_entry_hash(row) != row["entry_hash"]:
                report.update(
                    ok=False,
                    broken_at=row["id"],
                    reason="entry_hash does not match the row contents: the row was edited",
                )
                return report
            expected_prev = row["entry_hash"]

        return report


def _audit_filter_clause(
    since: Optional[str] = None,
    until: Optional[str] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    target: Optional[str] = None,
    result: Optional[str] = None,
) -> tuple:
    """Build the WHERE clause shared by listing and counting."""
    clauses: List[str] = []
    params: List[Any] = []
    if since:
        clauses.append("ts >= ?")
        params.append(_normalise_audit_ts(since))
    if until:
        clauses.append("ts <= ?")
        params.append(_normalise_audit_ts(until))
    if actor:
        clauses.append("actor = ?")
        params.append(actor)
    if action:
        clauses.append("action LIKE ?")
        params.append(f"{action}%")
    if target:
        clauses.append("(target_id = ? OR revision_id = ?)")
        params.extend([target, target])
    if result:
        clauses.append("result = ?")
        params.append(result)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _checkpoint_anchor(rows: List[Dict[str, Any]], first_prev: str) -> Optional[str]:
    """Hash recorded by a retention checkpoint for the row before the first survivor."""
    """Hash a retention checkpoint recorded for the row before the first survivor."""
    for row in rows:
        if row["action"] != "audit.checkpoint":
            continue
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except ValueError:
            continue
        if detail.get("pruned_through_hash") == first_prev:
            return first_prev
    return None


COMPONENT_TYPES = {
    "receivers/": "receiver",
    "processors/": "processor",
    "exporters/": "exporter",
    "extensions/": "extension",
    "connectors/": "connector",
}

COMPONENT_SHORT_NAMES = {
    "receiver": "receiver",
    "receivers": "receiver",
    "processor": "processor",
    "processors": "processor",
    "exporter": "exporter",
    "exporters": "exporter",
    "extension": "extension",
    "extensions": "extension",
    "connector": "connector",
    "connectors": "connector",
}

KNOWN_COMPONENTS = {
    "otlp": "receiver",
    "jaeger": "receiver",
    "prometheus": "receiver",
    "zipkin": "receiver",
    "statsd": "receiver",
    "host_metrics": "receiver",
    "batch": "processor",
    "memory_limiter": "processor",
    "filter": "processor",
    "transform": "processor",
    "resourcedetection": "processor",
    "debug": "exporter",
    "logging": "exporter",
    "prometheusremotewrite": "exporter",
    "otlphttp": "exporter",
    "opamp": "extension",
    "zpages": "extension",
    "health_check": "extension",
    "pprof": "extension",
}


CAPABILITY_TAGS = {
    0x00000001: ("Reports Status", "📊"),
    0x00000002: ("Accepts Remote Config", "⚙️"),
    0x00000004: ("Reports Effective Config", "📋"),
    0x00000008: ("Accepts Packages", "📦"),
    0x00000010: ("Reports Package Statuses", "📜"),
    0x00000020: ("Reports Own Traces", "🧭"),
    0x00000040: ("Reports Own Metrics", "📈"),
    0x00000080: ("Reports Own Logs", "📝"),
    0x00000100: ("Accepts OpAMP Connection", "🔌"),
    0x00000200: ("Accepts Other Connections", "🌐"),
    0x00000400: ("Accepts Restart", "🔄"),
    0x00000800: ("Reports Health", "❤️"),
    0x00001000: ("Reports Remote Config", "🛰️"),
    0x00002000: ("Reports Heartbeat", "💓"),
    0x00004000: ("Reports Components", "🧩"),
    0x00008000: ("Reports Connection Status", "📡"),
}


# #69: cadence learning. A gap is only treated as a reporting interval once it is
# long enough not to be an artefact (an agent that sends two messages in one batch,
# or a registration followed immediately by the first report, says nothing about
# how often it polls), and the estimate is only trusted after two such gaps so a
# restart — where the first gap is measured from a *persisted* heartbeat — cannot
# halve it.
MIN_REPORT_GAP_SECONDS = 1.0
REPORT_INTERVAL_ALPHA = 0.3  # EWMA weight for the newest gap
TRUSTED_REPORT_SAMPLES = 2


@dataclass
class AgentState:
    instance_uid: bytes
    agent_id: str
    connected_at: datetime = field(default_factory=utcnow)
    last_heartbeat: datetime = field(default_factory=utcnow)
    healthy: Optional[bool] = None
    capabilities: int = 0
    description: Dict[str, Any] = field(default_factory=dict)
    effective_config: Optional[str] = None
    remote_config_status: str = "UNSET"
    package_statuses: Dict[str, Any] = field(default_factory=dict)
    available_components: Dict[str, Any] = field(default_factory=dict)
    health: Dict[str, Any] = field(default_factory=dict)
    compliance: Optional[Dict[str, Any]] = None
    # #69: the agent's own reporting cadence, learned from the gaps between its
    # messages. A fixed timeout flaps every agent whose `polling_interval` is
    # longer than it (delete → re-register every cycle → `stale_agent` +
    # `new_agent` alerts forever), and the collector's interval is user-set.
    report_interval: Optional[float] = None  # EWMA of observed gaps, seconds
    report_samples: int = 0  # informative gaps seen (see record_report)
    # #81: the spec's reconciliation MUST compares hashes, so both halves of the
    # agent's RemoteConfigStatus are kept. Hex here, base64 on the wire.
    last_remote_config_hash: Optional[str] = None
    remote_config_error: Optional[str] = None
    # #70: the last `AgentToServer.sequence_num` we accepted, so the next one can be
    # checked for "exactly one greater". None = no baseline yet (see sequence.is_gap).
    last_sequence_num: Optional[int] = None
    # #75: set only on an agent whose uid was a duplicate of another agent's. The hex
    # of the uid it presented and the signal that convicted it, so the fleet view can
    # explain an id change instead of showing a row that silently appeared.
    uid_reissued_from: Optional[str] = None
    uid_reissue_reason: Optional[str] = None
    # #84: a server->agent command waiting to ride the agent's next poll. Persisted,
    # because HTTP has no push and a restart command lost on reboot is a silent no-op.
    # Cleared the moment it is delivered: re-delivering a restart every poll is a
    # crash loop.
    pending_command: Optional[Dict[str, Any]] = None
    command_delivered_at: Optional[str] = None
    # #98: the sealed body this row was loaded from, kept only when it could not be
    # opened with the keys this process has (see SQLiteAgentStore._sealed_...).
    sealed_effective_config: Optional[str] = field(default=None, repr=False)
    config_error: Optional[str] = None
    # (raw json, redacted json, paths) — redaction is a per-request cost otherwise,
    # and `GET /agents` walks every agent (#98, #109).
    _redacted_effective_config: Optional[tuple] = field(default=None, repr=False, compare=False)
    # (the effective_config it was derived from, the usage map) — see _config_usage.
    _usage_cache: Optional[tuple] = field(default=None, repr=False, compare=False)

    def record_report(self, now: Optional[datetime] = None) -> None:
        """Record that the agent just reported, and learn from the gap (#69).

        Called from the OpAMP message path only. `AGENT_REGISTRY.update()` no longer
        touches `last_heartbeat`, so a server-side mutation (a compliance check, say)
        cannot keep a dead agent looking alive.
        """
        now = now or utcnow()
        gap = (now - self.last_heartbeat).total_seconds()
        if gap >= MIN_REPORT_GAP_SECONDS:
            self.report_samples += 1
            if self.report_interval is None:
                self.report_interval = gap
            else:
                self.report_interval += REPORT_INTERVAL_ALPHA * (gap - self.report_interval)
        self.last_heartbeat = now

    def timeout_seconds(
        self,
        floor: float,
        margin: float = 3.0,
        first_contact: float = 900.0,
    ) -> float:
        """How long this agent may stay silent before it is removed (#69).

        `max(floor, margin × observed_interval)`. An agent whose cadence is not yet
        trusted gets `first_contact` instead: until one full gap has been seen, "slow
        poller" and "died on startup" are the same observation, and guessing low is
        what makes the dashboard flap.
        """
        if self.report_samples < TRUSTED_REPORT_SAMPLES or not self.report_interval:
            return max(floor, first_contact)
        return max(floor, margin * self.report_interval)

    def is_stale(
        self,
        now: Optional[datetime] = None,
        floor: float = 180.0,
        margin: float = 3.0,
        first_contact: float = 900.0,
    ) -> bool:
        """Has this agent been silent longer than its own budget? (#69, #101)

        One implementation shared by the stale-agent sweep (`server/main.py`) and the
        rollout health gate: a gate that used a *different* idea of "silent" than the
        reaper would either promote over a dead agent or pause a healthy one.
        """
        now = now or utcnow()
        timeout = self.timeout_seconds(floor, margin, first_contact)
        heartbeat = self.last_heartbeat
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        return (now - heartbeat).total_seconds() > timeout

    @property
    def capability_tags(self) -> list[dict]:
        return [
            {"label": label, "icon": icon}
            for bit, (label, icon) in CAPABILITY_TAGS.items()
            if self.capabilities & bit
        ]

    def _detect_type(self, comp_id: str) -> str:
        for prefix, type_name in COMPONENT_TYPES.items():
            if comp_id.startswith(prefix):
                return type_name
        
        if comp_id in KNOWN_COMPONENTS:
            return KNOWN_COMPONENTS[comp_id]
        
        if '/' in comp_id:
            first_part = comp_id.split('/')[0]
            if first_part in COMPONENT_SHORT_NAMES:
                return COMPONENT_SHORT_NAMES[first_part]
            if first_part in KNOWN_COMPONENTS:
                return KNOWN_COMPONENTS[first_part]
        
        return "unknown"

    def _extract_version(self, comp_data: dict) -> str:
        try:
            metadata = comp_data.get("metadata", [])
            for meta in metadata:
                if meta.get("key") == "code.namespace":
                    value = meta.get("value", {})
                    string_value = value.get("stringValue", "")
                    if string_value:
                        parts = string_value.rsplit(" v", 1)
                        if len(parts) == 2:
                            return parts[1]
                    return string_value.split(" v")[-1] if " v" in string_value else ""
            return ""
        except Exception:
            return ""

    @property
    def components(self) -> dict:
        groups = defaultdict(list)
        
        type_mapping = {
            "receivers": "receiver",
            "processors": "processor",
            "exporters": "exporter",
            "extensions": "extension",
            "connectors": "connector",
        }
        
        used_by_type = self._config_usage()
        
        if not used_by_type:
            used_by_type = self._parse_health_components()
        
        components_container = self.available_components.get("components", {})
        for comp_type_key, comp_type in type_mapping.items():
            if comp_type_key in components_container:
                comps_dict = components_container[comp_type_key].get("subComponentMap", {})
                used_in_pipeline = used_by_type.get(comp_type, set())
                for comp_id, comp_data in comps_dict.items():
                    is_used = comp_id in used_in_pipeline
                    version = self._extract_version(comp_data)
                    groups[comp_type].append({
                        "id": comp_id,
                        "version": version,
                        "used": is_used
                    })
        
        for comp_type in groups:
            groups[comp_type].sort(key=lambda c: (not c["used"], c["id"]))
        
        return dict(groups)
    
    def _config_usage(self) -> Dict[str, set]:
        """Components used by the reported config, cached against its exact text (#68).

        Reading every config object is more work than reading `configMap[""]`, and
        `components` is recomputed for every agent on every `GET /agents`. The cache is
        keyed on the stored JSON itself, so a new report invalidates it and nothing
        else has to remember to.
        """
        if not self.effective_config:
            return {}
        cached = self._usage_cache
        if cached is not None and cached[0] == self.effective_config:
            return cached[1]
        try:
            usage = parse_effective_config(json.loads(self.effective_config))
        except Exception as exc:  # a malformed report must not 500 a listing
            logger.warning(f"Failed to parse effective_config: {exc}")
            usage = {}
        self._usage_cache = (self.effective_config, usage)
        return usage

    def _parse_health_components(self) -> Dict[str, set]:
        used_by_type = defaultdict(set)
        health_map = self.health.get("componentHealthMap", {})
        
        for key, value in health_map.items():
            if key.startswith("pipeline:"):
                nested_map = value.get("componentHealthMap", {})
                for comp_key in nested_map:
                    if ":" in comp_key:
                        comp_type, comp_id = comp_key.split(":", 1)
                        used_by_type[comp_type].add(comp_id)
            elif key == "extensions":
                nested_map = value.get("componentHealthMap", {})
                for comp_key in nested_map:
                    if ":" in comp_key:
                        _, comp_id = comp_key.split(":", 1)
                        used_by_type["extension"].add(comp_id)
        
        return dict(used_by_type)

    def get_attr(self, key: str) -> Optional[str]:
        """Return the stringified value of a description attribute.

        Looks in both identifyingAttributes and nonIdentifyingAttributes (the
        same sources the UI "By Property" grouping uses). Returns None when the
        attribute is absent or its value is not a scalar (e.g. array/kvlist).
        """
        attrs = self.description.get("identifyingAttributes", []) + self.description.get(
            "nonIdentifyingAttributes", []
        )
        for attr in attrs:
            if attr.get("key") == key:
                return _stringify_any_value(attr.get("value"))
        return None

    def matches_filters(
        self,
        healthy: Optional[str] = None,
        remote_config_status: Optional[str] = None,
        attributes: Optional[Dict[str, list]] = None,
    ) -> bool:
        """Return True when the agent satisfies all the given filters.

        - ``healthy``: "true" / "false" (strict bool match) or "unknown"
          (no health reported), mirroring the UI Agents page grouping.
        - ``remote_config_status``: case-insensitive string match.
        - ``attributes``: mapping of description attribute key -> acceptable
          values (any match wins, i.e. OR); agents missing the attribute fail.
        """
        if healthy is not None:
            if healthy == "unknown":
                if self.healthy is not None:
                    return False
            elif self.healthy != (healthy == "true"):
                return False

        if remote_config_status is not None and (
            (self.remote_config_status or "").lower() != remote_config_status.lower()
        ):
            return False

        for key, values in (attributes or {}).items():
            actual = self.get_attr(key)
            if actual is None or not any(
                value.lower() == actual.lower() for value in values if value is not None
            ):
                return False

        return True

    def redacted_effective_config(self) -> tuple:
        """`(json, redacted_paths)` for the effective config, cached per raw body.

        Redaction is a parse + walk of a ~10 KB document; doing it for every agent
        on every `GET /agents` would put it back on the hot path #109 just removed
        it from. The cache key is the raw string itself, so it self-invalidates.
        """
        raw = self.effective_config
        cached = self._redacted_effective_config
        if cached is not None and cached[0] == raw:
            return cached[1], cached[2]
        redacted, paths = redaction.redact_effective_config(raw)
        self._redacted_effective_config = (raw, redacted, paths)
        return redacted, paths

    def to_dict(self) -> Dict[str, Any]:
        """API shape. **Redacted by default** — this is the single choke point (#98).

        Every read surface (`/agents`, `/agent/{id}`, reports, the UI) goes through
        here, so a config containing a password renders masked everywhere without
        each caller remembering. The audited reveal path in `main.py` re-reads the
        raw value deliberately, not by accident.
        """
        effective_config, redacted_paths = self.redacted_effective_config()
        return {
            "id": self.agent_id,
            "healthy": self.healthy,
            "connected_at": self.connected_at.isoformat(),
            "last_heartbeat": self.last_heartbeat.isoformat(),
            "report_interval_seconds": (
                round(self.report_interval, 3) if self.report_interval is not None else None
            ),
            "capabilities": self.capabilities,
            "capability_tags": self.capability_tags,
            "description": self.description,
            "effective_config": effective_config,
            "effective_config_redacted": bool(redacted_paths),
            "redacted_paths": redacted_paths,
            "config_error": self.config_error,
            "remote_config_status": self.remote_config_status,
            "last_remote_config_hash": self.last_remote_config_hash,
            "remote_config_error": self.remote_config_error,
            # #70: exposed so "why did the server ask for full state?" is answerable
            # from the API instead of only from a log line.
            "last_sequence_num": self.last_sequence_num,
            # #84: "restart pending / delivered" is an operator question, so it is
            # answered by the agent view rather than only by a log line. `None` in both
            # fields means no command was ever queued.
            "pending_command": self.pending_command,
            "command_delivered_at": self.command_delivered_at,
            # #75: `None` for every agent whose uid was never re-issued; otherwise the
            # uid the agent presented and the signal that convicted it.
            "uid_reissued_from": self.uid_reissued_from,
            "uid_reissue_reason": self.uid_reissue_reason,
            "package_statuses": self.package_statuses,
            "available_components": self.available_components,
            "components": self.components,
            "compliance": self.compliance,
        }


class AgentRegistry:
    """In-memory agent registry backed by SQLite (`agents` table).

    Writes are the capacity ceiling on the OpAMP message path (#109), so callers
    may batch: `update(persist=False)` applies fields in memory and `persist()`
    flushes the row once. `update()` returns whether anything actually changed so
    a no-op heartbeat can skip the write entirely.
    """

    def __init__(self):
        self._store = SQLiteAgentStore()
        self._agents: Dict[str, AgentState] = self._store.load_all()
        # agent_id -> monotonic time of the last successful persist
        self._persisted_at: Dict[str, float] = {}
        # agent_id -> monotonic time we last asked it for a full status report (#70)
        self._full_state_requested_at: Dict[str, float] = {}

    def register(self, agent_id: str, state: AgentState, persist: bool = True) -> None:
        self._agents[agent_id] = state
        if persist:
            self.persist(agent_id)

    def get(self, agent_id: str) -> Optional[AgentState]:
        return self._agents.get(agent_id)

    def update(self, agent_id: str, persist: bool = True, **kwargs) -> bool:
        """Apply `kwargs` to the in-memory state; return True if a value changed.

        This is a *server-side* mutation (a compliance result, say): it deliberately
        does not touch `last_heartbeat`, because a liveness signal must come from the
        agent (#69). Use `record_report()` for that.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            return False
        changed = False
        for key, value in kwargs.items():
            if getattr(agent, key, None) != value:
                setattr(agent, key, value)
                changed = True
        if persist:
            self.persist(agent_id)
        return changed

    def record_report(self, agent_id: str, now: Optional[datetime] = None) -> None:
        """The agent sent a message: move `last_heartbeat`, learn its cadence (#69).

        In-memory only — the message path persists once, after it has applied every
        field group (#109).
        """
        agent = self._agents.get(agent_id)
        if agent is not None:
            agent.record_report(now)

    def persist(self, agent_id: str) -> None:
        """Write the whole row once (the only place agents are written)."""
        agent = self._agents.get(agent_id)
        if agent is None:
            return
        self._store.upsert(agent)
        self._persisted_at[agent_id] = time.monotonic()

    def heartbeat_persist_due(self, agent_id: str, min_interval: float) -> bool:
        """True if the stored `last_heartbeat` is stale enough to be worth a write.

        Bounds the recovery skew after a restart to `min_interval` while keeping
        steady state at ~0 writes.
        """
        last = self._persisted_at.get(agent_id)
        return last is None or (time.monotonic() - last) >= min_interval

    def claim_full_state_request(
        self, agent_id: str, min_interval: float, now: Optional[float] = None
    ) -> bool:
        """True if a `ReportFullState` ask for this agent is allowed right now (#70).

        Records the request when it grants one, so a flapping agent cannot turn a
        gap into a storm: without this, every message with a broken counter would
        ask for full state, and the server would amplify one agent's problem across
        the whole connection's queue. Rate limiting is per agent and in-memory on
        purpose — it is a traffic-shaping decision, not state worth persisting.
        """
        now = time.monotonic() if now is None else now
        last = self._full_state_requested_at.get(agent_id)
        if last is not None and (now - last) < min_interval:
            return False
        self._full_state_requested_at[agent_id] = now
        return True

    def remove(self, agent_id: str) -> bool:
        if agent_id in self._agents:
            del self._agents[agent_id]
            self._persisted_at.pop(agent_id, None)
            self._full_state_requested_at.pop(agent_id, None)
            self._store.remove(agent_id)
            return True
        return False

    def list_all(self) -> list:
        return list(self._agents.values())

    @property
    def count(self) -> int:
        return len(self._agents)


AGENT_REGISTRY = AgentRegistry()
