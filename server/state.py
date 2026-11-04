import hashlib
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Mapping, Sequence
from loguru import logger

from server import crypto, redaction
from server import profiles
# #68: effective-config reading lives in one shared module so `server/` and the UI
# cannot drift again. Re-exported here because `from server.state import
# parse_effective_config` is the old import path.
from server.effective_config import parse_effective_config  # noqa: F401 (re-export)


def utcnow():
    return datetime.now(timezone.utc)


# #111: keys a filter document may use that are *not* description attributes.
# `status` is the older spelling of `remote_config_status`. Kept next to the one
# implementation that splits them so a group definition and `GET /agents` can never
# disagree about what `healthy` means.
RESERVED_FILTER_KEYS = frozenset({"healthy", "status", "remote_config_status"})


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

# #126: outcomes of `SQLiteCredentialStore.register_agent`. Strings rather than an enum
# because they are also the `reason` on the metric and in the audit entry.
AGENT_LIMIT_OK = "ok"
AGENT_LIMIT_REACHED = "agent_limit"


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
                    command_delivered_at TEXT,
                    last_requested_components_hash TEXT,
                    components_request_ignored INTEGER DEFAULT 0,
                    custom_capabilities TEXT,
                    pending_custom_message TEXT,
                    custom_message_delivered_at TEXT,
                    agent_type TEXT
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
            # #85: a custom capability list is *replaced*, never appended (the spec
            # says the message carries the whole list), so one JSON column is the
            # whole state. `pending_custom_message` is the outbound queue: persisted
            # for the same reason `pending_command` is (HTTP has no push, and a
            # message lost on restart is a silent no-op).
            if "custom_capabilities" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN custom_capabilities TEXT")
            if "pending_custom_message" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN pending_custom_message TEXT")
            if "custom_message_delivered_at" not in columns:
                conn.execute(
                    "ALTER TABLE agents ADD COLUMN custom_message_delivered_at TEXT"
                )
            # #77: the `AvailableComponents.hash` we last asked the agent to expand,
            # so the `ReportAvailableComponents` ask is one-shot per hash instead of a
            # request on every poll. Persisted because a restart must not re-ask a
            # hash the agent already answered (or already ignored).
            if "last_requested_components_hash" not in columns:
                conn.execute(
                    "ALTER TABLE agents ADD COLUMN last_requested_components_hash TEXT"
                )
            if "components_request_ignored" not in columns:
                conn.execute(
                    "ALTER TABLE agents ADD COLUMN components_request_ignored INTEGER"
                    " DEFAULT 0"
                )
            # #118: an operator's pin of the agent's *type*. NULL means auto-detect,
            # which is what every pre-#118 row (and every unpinned agent) gets, so no
            # backfill is needed and nothing behaves differently after the upgrade.
            if "agent_type" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN agent_type TEXT")

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
                                   uid_reissue_reason, last_requested_components_hash,
                                   components_request_ignored, custom_capabilities,
                                   pending_custom_message, custom_message_delivered_at,
                                   agent_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    uid_reissue_reason=excluded.uid_reissue_reason,
                    last_requested_components_hash=excluded.last_requested_components_hash,
                    components_request_ignored=excluded.components_request_ignored,
                    custom_capabilities=excluded.custom_capabilities,
                    pending_custom_message=excluded.pending_custom_message,
                    custom_message_delivered_at=excluded.custom_message_delivered_at,
                    agent_type=excluded.agent_type
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
            state.last_requested_components_hash,
            state.components_request_ignored,
            json.dumps(state.custom_capabilities or []),
            json.dumps(state.pending_custom_message)
            if state.pending_custom_message
            else None,
            state.custom_message_delivered_at,
            state.agent_type,
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
            last_requested_components_hash=_optional_column(
                row, "last_requested_components_hash"
            ),
            components_request_ignored=(
                _optional_column(row, "components_request_ignored") or 0
            ),
            custom_capabilities=json.loads(
                _optional_column(row, "custom_capabilities") or "[]"
            ),
            pending_custom_message=json.loads(
                _optional_column(row, "pending_custom_message") or "null"
            ),
            custom_message_delivered_at=_optional_column(
                row, "custom_message_delivered_at"
            ),
            # #118: NULL on every row written before this column existed, which is
            # exactly "auto-detect".
            agent_type=_optional_column(row, "agent_type"),
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
    """OpAMP agent tokens (#94, #126): one row per issued token.

    Only the SHA-256 of a token is stored — the token itself exists in the API
    response that created it and nowhere else. Rows are never deleted, so
    `revoked_at`/`last_seen_at` stay available as the audit trail an operator
    needs to watch a fleet finish a cutover.

    Two kinds of row, and the difference is `agent_id`:

    * **bound** (`agent_id` = a hex uid): the #94 credential. It *is* that agent's
      identity, and a message claiming another uid is refused. Rotation uses
      `retire_on_use`: the outgoing row retires automatically the first time its
      replacement authenticates, which is what makes rotation zero-downtime without
      a timer (a timer would either cut too early, breaking a collector that has not
      reloaded yet, or leave the old token alive forever).
    * **fleet** (`agent_id` = ``FLEET_TOKEN``, the empty string): #126. One token, many
      agents, up to `max_agents`. It authorises access; it is *not* an identity, so the
      uid in the body stands. The empty string rather than NULL is deliberate: it keeps
      the column `NOT NULL` (so no table rebuild to upgrade an existing database) and
      it cannot collide with a real uid, which is always hex.

    `token_id` is the public handle (`agtok_…`) that listings, revocation and audit
    entries key off. A `token_hash` must never be one: it is the verifier, so publishing
    it — even hashed — would turn a listing endpoint into an oracle.

    Timestamps are ISO-8601 UTC strings, like every other table here.
    """

    #: `agent_id` value for a token that is not bound to one agent (#126).
    FLEET_TOKEN = ""

    #: Columns added after the first release. `PRAGMA table_info` + `ALTER TABLE` is
    #: how this project upgrades in place (no migration framework, one file).
    _ADDED_COLUMNS = (
        ("token_id", "TEXT"),
        ("expires_at", "TEXT"),
        ("max_agents", "INTEGER"),
    )

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # Before any write: WAL cannot be set inside a transaction, and the ALTER
            # TABLE / UPDATE below open one on an existing database.
            conn.execute("PRAGMA journal_mode=WAL")
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
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(agent_credentials)")
            }
            for name, sql_type in self._ADDED_COLUMNS:
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE agent_credentials ADD COLUMN {name} {sql_type}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS agent_credentials_agent_idx "
                "ON agent_credentials(agent_id)"
            )
            # #126: which uids a token has been used by. The cap counts these, so the
            # count survives a restart and a re-labelled token; the pair is the key so
            # one agent can never consume two slots.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_credential_agents (
                    token_hash TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY (token_hash, agent_id)
                )
            """)
            # Rows minted before #126 have no handle. Derive one from the hash: it is
            # stable across restarts and only ever exposed as an opaque id.
            conn.execute(
                "UPDATE agent_credentials SET token_id='agtok_' || substr(token_hash, 1, 16) "
                "WHERE token_id IS NULL"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS agent_credentials_token_id_idx "
                "ON agent_credentials(token_id)"
            )
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
        agent_id: str = FLEET_TOKEN,
        label: str = "",
        retire_on_use: Optional[str] = None,
        token_id: Optional[str] = None,
        expires_at: Optional[str] = None,
        max_agents: Optional[int] = None,
    ) -> None:
        """Insert one token. `agent_id=""` (the default) is a fleet token (#126)."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_credentials "
                "(token_hash, agent_id, label, created_at, retire_on_use, token_id, "
                "expires_at, max_agents) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token_hash,
                    agent_id,
                    label,
                    utcnow().isoformat(),
                    retire_on_use,
                    token_id or f"agtok_{token_hash[:16]}",
                    expires_at,
                    max_agents,
                ),
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

    # -- #126: fleet tokens -------------------------------------------------

    def list_all(self) -> List[Dict[str, Any]]:
        """Every token, newest first, with its live use count.

        The count comes from `agent_credential_agents` (distinct uids that presented
        the token), so "3 of 15" is the same number the cap enforces — a second
        source of truth would drift the moment a slot is refused.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM agent_credential_agents a "
                "WHERE a.token_hash = c.token_hash) AS agents_used "
                "FROM agent_credentials c ORDER BY c.created_at DESC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def by_token_id(self, token_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_credentials WHERE token_id=?", (token_id,)
            ).fetchone()
        return self._row(row) if row else None

    def revoke_by_token_id(self, token_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_credentials SET revoked_at=? "
                "WHERE token_id=? AND revoked_at IS NULL",
                (utcnow().isoformat(), token_id),
            )
            return bool(cursor.rowcount)

    def agents_for_token(self, token_hash: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_id FROM agent_credential_agents WHERE token_hash=? "
                "ORDER BY first_seen_at",
                (token_hash,),
            ).fetchall()
        return [row["agent_id"] for row in rows]

    def tokens_for_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        """Fleet tokens this uid has presented, newest first.

        A fleet token is not the agent's identity, but "which token let this agent in"
        is the first question when a cap is hit or a token leaks.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.* FROM agent_credentials c "
                "JOIN agent_credential_agents a ON a.token_hash = c.token_hash "
                "WHERE a.agent_id=? ORDER BY c.created_at DESC",
                (agent_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def register_agent(
        self,
        token_hash: str,
        agent_id: str,
        max_agents: Optional[int],
    ) -> str:
        """Count `agent_id` against this token's cap. Returns `ok` or `agent_limit`.

        `BEGIN IMMEDIATE` takes the write lock *before* the count, so two agents
        racing for the last slot cannot both be admitted: the second one blocks, then
        reads the post-insert count and is refused. A plain `with conn:` block would
        defer the write until the INSERT and let both pass the check.

        A uid that has already been seen is a no-op, so this runs on every message
        without consuming a slot twice (the caller still caches the answer — see
        `agent_auth` — because this is a write transaction on the auth path).
        """
        conn = self._connect()
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            seen = conn.execute(
                "SELECT 1 FROM agent_credential_agents "
                "WHERE token_hash=? AND agent_id=?",
                (token_hash, agent_id),
            ).fetchone()
            if seen:
                conn.execute("COMMIT")
                return AGENT_LIMIT_OK
            if max_agents is not None:
                used = conn.execute(
                    "SELECT COUNT(*) FROM agent_credential_agents WHERE token_hash=?",
                    (token_hash,),
                ).fetchone()[0]
                if used >= int(max_agents):
                    conn.execute("COMMIT")
                    return AGENT_LIMIT_REACHED
            conn.execute(
                "INSERT INTO agent_credential_agents "
                "(token_hash, agent_id, first_seen_at) VALUES (?, ?, ?)",
                (token_hash, agent_id, utcnow().isoformat()),
            )
            conn.execute("COMMIT")
            return AGENT_LIMIT_OK
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


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
        # #111: a group-scoped revision records *which group* and *what that group
        # meant* when it was approved. The snapshot is what makes "pushed to
        # prod-emea as defined on 2026-03-04; the definition has since changed"
        # answerable without rewriting history every time somebody edits a group.
        "group_id": "TEXT",
        "group_snapshot_json": "TEXT",
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
        group_id: Optional[str] = None,
        group_snapshot_json: Optional[str] = None,
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
                "scope_kind, scope_json, note, parent_revision_id, verdict_json, "
                "group_id, group_snapshot_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    group_id,
                    group_snapshot_json,
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


class GroupError(ValueError):
    """A refused group operation, carrying the HTTP status the route should use."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


class SQLiteGroupStore:
    """Named collector groups (#111): a **saved selector**, never a member list.

    Membership is resolved at use time by `AgentState.matches_scope()`, the same
    implementation `GET /agents` and a config `filter` scope use. Storing members
    would be a second source of truth that goes stale the moment an agent's
    attributes change, which is exactly the drift a named selector is supposed to
    remove.

    Two tables:

    ``collector_groups``
        The definition (filters + `include`/`exclude` pins), its owner and the
        lifecycle. Groups are **archived, never deleted**: revisions and rollouts
        reference `group_id`, and the audit log is append-only, so a hard delete
        would leave history pointing at nothing.

    ``collector_group_resolutions``
        What the group meant, when — `preview`, `push` and `propose` evaluations.
        This is what makes "it used to be 12 agents" checkable, and it is the
        baseline the shrink guard compares against (`preview`'s `dropped` diff).

    Validation lives here, not in the routes: a name, a definition or a pin that
    the store would not accept must fail the same way whether it arrived from the
    API, the CLI, MCP or a file.
    """

    NAME_MIN = 2
    NAME_MAX = 63
    # Exact-match semantics, stated in the UI: no globs, no regex, no prefix. `prod-*`
    # is the first thing a human types, so the allowed characters are deliberately
    # narrow enough that it cannot be mistaken for a pattern.
    NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,62}$"
    RESERVED_NAMES = frozenset({"healthy", "status", "remote_config_status"})
    SOURCES = ("api", "file")
    REASONS = ("preview", "propose", "push", "approve")

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._create_schema()

    def reopen(self, db_path) -> None:
        """Point this (module-level) store at another database and create the schema.

        The singleton is bound into default arguments across `groups`/`config_control`,
        so a test cannot swap the object; it has to move the path. Creating the schema
        here keeps every other store's `__init__` contract intact.
        """
        self.db_path = Path(db_path)
        self._create_schema()

    def _create_schema(self) -> None:
        db_path = self.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collector_groups (
                    group_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL UNIQUE,
                    definition_json TEXT NOT NULL,
                    include_json TEXT NOT NULL DEFAULT '[]',
                    exclude_json TEXT NOT NULL DEFAULT '[]',
                    description TEXT,
                    owner TEXT,
                    source TEXT NOT NULL DEFAULT 'api',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_by TEXT,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT,
                    archived_by TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collector_group_resolutions (
                    group_id TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    agent_ids_json TEXT NOT NULL,
                    agent_count INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT,
                    PRIMARY KEY (group_id, evaluated_at)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS collector_group_resolutions_idx "
                "ON collector_group_resolutions(group_id, evaluated_at DESC)"
            )
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ------------------------------------------------------------- validation

    @classmethod
    def validate_name(cls, name: str) -> str:
        # Lowercased before validation: a name is an identifier, and
        # `Prod-EMEA` vs `prod-emea` must collide as a duplicate (409) rather than
        # read as two different groups. The stored form is canonical lowercase.
        candidate = (name or "").strip().lower()
        if not re.fullmatch(cls.NAME_PATTERN, candidate):
            raise GroupError(
                422,
                f"invalid group name {candidate!r}: use {cls.NAME_MIN}-{cls.NAME_MAX} "
                "lowercase characters from a-z, 0-9, '.', '_' and '-', starting with a "
                "letter or digit (globs and spaces are not names)",
            )
        if candidate.lower() in cls.RESERVED_NAMES:
            raise GroupError(
                422,
                f"{candidate!r} is a reserved filter key, not a group name; a group must "
                "stay visibly different from an attribute filter",
            )
        return candidate

    @staticmethod
    def validate_definition(definition: Any) -> Dict[str, Any]:
        """A definition is a filter document, and it may not be empty.

        `{}` would match every agent, so a group would be "the whole fleet" without
        anyone typing that. That is the one selector this project never lets anybody
        write by accident (a missing scope is never "all agents" either).
        """
        if not isinstance(definition, dict):
            raise GroupError(422, "definition must be an object of filter keys and values")
        cleaned: Dict[str, Any] = {}
        for key, value in definition.items():
            name = str(key).strip()
            if not name:
                raise GroupError(422, "definition keys must not be empty")
            if isinstance(value, (dict, list)):
                if isinstance(value, list) and value and not all(
                    isinstance(item, (str, int, float, bool)) for item in value
                ):
                    raise GroupError(
                        422,
                        f"definition[{name!r}] must be a scalar or a list of scalars; "
                        "matching is exact equality against the agent's attributes",
                    )
                if isinstance(value, list) and not value:
                    raise GroupError(422, f"definition[{name!r}] must not be an empty list")
                if isinstance(value, dict):
                    raise GroupError(
                        422,
                        f"definition[{name!r}] must be a scalar or a list of scalars",
                    )
            if value is None:
                raise GroupError(422, f"definition[{name!r}] must not be null")
            cleaned[name] = value
        if not cleaned:
            raise GroupError(
                422,
                "a group definition needs at least one filter; an empty definition "
                "would mean the whole fleet",
            )
        return cleaned

    @classmethod
    def validate_pins(cls, pins: Any, *, label: str) -> List[str]:
        if pins is None:
            return []
        if not isinstance(pins, (list, tuple)):
            raise GroupError(422, f"{label} must be a list of agent ids")
        out: List[str] = []
        for raw in pins:
            value = str(raw).strip()
            if not value:
                raise GroupError(422, f"{label} must not contain an empty agent id")
            if value not in out:
                out.append(value)
        return out

    # ------------------------------------------------------------- decoding

    @staticmethod
    def _row(row) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        record = {key: row[key] for key in row.keys()}
        for field_name in ("definition_json", "include_json", "exclude_json"):
            raw = record.pop(field_name, None)
            key = field_name[: -len("_json")]
            try:
                parsed = json.loads(raw) if raw else ({} if key == "definition" else [])
            except (TypeError, ValueError):
                parsed = {} if key == "definition" else []
            record[key] = parsed
        return record

    # --------------------------------------------------------------- writes

    def create(
        self,
        *,
        name: str,
        definition: Dict[str, Any],
        include: Optional[Sequence[str]] = None,
        exclude: Optional[Sequence[str]] = None,
        description: Optional[str] = None,
        owner: Optional[str] = None,
        source: str = "api",
        created_by: str = "system",
    ) -> Dict[str, Any]:
        """Create a group. Duplicate names are refused case-insensitively."""
        name = self.validate_name(name)
        definition = self.validate_definition(definition)
        include_list = self.validate_pins(include, label="include")
        exclude_list = self.validate_pins(exclude, label="exclude")
        if set(include_list) & set(exclude_list):
            raise GroupError(
                422,
                "an agent cannot be both pinned in and pinned out of the same group",
            )
        if source not in self.SOURCES:
            raise GroupError(422, f"source must be one of {', '.join(self.SOURCES)}")
        now = utcnow().isoformat()
        group_id = self.new_group_id()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT group_id FROM collector_groups WHERE name_key=?", (name.lower(),)
            ).fetchone()
            if existing is not None:
                raise GroupError(
                    409,
                    {
                        "message": f"a group named {name!r} already exists",
                        "group_id": existing["group_id"],
                        "hint": "names are unique regardless of case; update that group "
                        "or choose another name",
                    },
                )
            conn.execute(
                "INSERT INTO collector_groups (group_id, name, name_key, "
                "definition_json, include_json, exclude_json, description, owner, "
                "source, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    group_id,
                    name,
                    name.lower(),
                    json.dumps(definition, sort_keys=True),
                    json.dumps(include_list),
                    json.dumps(exclude_list),
                    description,
                    owner,
                    source,
                    created_by,
                    now,
                    now,
                ),
            )
        return self.get(group_id) or {}

    def compute_changes(
        self,
        current: Mapping[str, Any],
        *,
        name: Optional[str] = None,
        definition: Optional[Dict[str, Any]] = None,
        include: Optional[Sequence[str]] = None,
        exclude: Optional[Sequence[str]] = None,
        description: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> tuple:
        """Validate an edit and compute `(updates, diff)` **without writing**.

        Split out of :meth:`update` so a route can write its audit row — carrying the
        definition diff — *before* the mutation (#97's pre-write ordering). A diff
        computed after the fact cannot be attached to the row that authorises it.
        """
        changes: Dict[str, Any] = {}
        updates: Dict[str, Any] = {}
        if name is not None and name != current["name"]:
            validated = self.validate_name(name)
            updates["name"] = validated
            updates["name_key"] = validated.lower()
            changes["name"] = {"from": current["name"], "to": validated}
        if definition is not None:
            validated = self.validate_definition(definition)
            if validated != current["definition"]:
                updates["definition_json"] = json.dumps(validated, sort_keys=True)
                changes["definition"] = diff_definitions(current["definition"], validated)
        if include is not None:
            validated_pins = self.validate_pins(include, label="include")
            if validated_pins != current["include"]:
                updates["include_json"] = json.dumps(validated_pins)
                changes["include"] = {
                    "added": [pin for pin in validated_pins if pin not in current["include"]],
                    "dropped": [pin for pin in current["include"] if pin not in validated_pins],
                }
        if exclude is not None:
            validated_pins = self.validate_pins(exclude, label="exclude")
            if validated_pins != current["exclude"]:
                updates["exclude_json"] = json.dumps(validated_pins)
                changes["exclude"] = {
                    "added": [pin for pin in validated_pins if pin not in current["exclude"]],
                    "dropped": [pin for pin in current["exclude"] if pin not in validated_pins],
                }
        if description is not None and description != current.get("description"):
            updates["description"] = description
            changes["description"] = {"from": current.get("description"), "to": description}
        if owner is not None and owner != current.get("owner"):
            updates["owner"] = owner
            changes["owner"] = {"from": current.get("owner"), "to": owner}

        include_final = current["include"] if include is None else (
            json.loads(updates.get("include_json") or "[]")
        )
        exclude_final = current["exclude"] if exclude is None else (
            json.loads(updates.get("exclude_json") or "[]")
        )
        if set(include_final) & set(exclude_final):
            raise GroupError(
                422, "an agent cannot be both pinned in and pinned out of the same group"
            )
        return updates, changes

    def update(
        self,
        group_id: str,
        *,
        actor: str,
        name: Optional[str] = None,
        definition: Optional[Dict[str, Any]] = None,
        include: Optional[Sequence[str]] = None,
        exclude: Optional[Sequence[str]] = None,
        description: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> tuple:
        """Edit a group, returning `(record, diff)`.

        The diff is the point: an edit silently changes the blast radius of every
        future push that names this group, so the audit row carries what changed
        rather than "changed". The previous definition is *not* mutable history —
        revisions keep their own `group_snapshot_json`.
        """
        current = self.get(group_id)
        if current is None:
            raise GroupError(404, f"group {group_id} not found")
        if current.get("archived_at"):
            raise GroupError(
                409,
                {
                    "message": f"group {current['name']!r} is archived",
                    "hint": "restore it first: an archived group is history, not a target",
                },
            )
        updates, changes = self.compute_changes(
            current,
            name=name,
            definition=definition,
            include=include,
            exclude=exclude,
            description=description,
            owner=owner,
        )
        if not updates:
            return current, {}
        updates["updated_by"] = actor
        updates["updated_at"] = utcnow().isoformat()
        assignments = ", ".join(f"{column}=?" for column in updates)
        try:
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE collector_groups SET {assignments} WHERE group_id=?",
                    (*updates.values(), group_id),
                )
        except sqlite3.IntegrityError as exc:  # the name_key unique index
            raise GroupError(
                409, f"a group named {updates.get('name')!r} already exists"
            ) from exc
        return self.get(group_id) or {}, changes

    def archive(self, group_id: str, *, actor: str) -> Dict[str, Any]:
        """Archive, never delete: revisions and rollouts still reference the id."""
        current = self.get(group_id)
        if current is None:
            raise GroupError(404, f"group {group_id} not found")
        if current.get("archived_at"):
            return current
        with self._connect() as conn:
            conn.execute(
                "UPDATE collector_groups SET archived_at=?, archived_by=?, "
                "updated_at=? WHERE group_id=?",
                (utcnow().isoformat(), actor, utcnow().isoformat(), group_id),
            )
        return self.get(group_id) or {}

    def restore(self, group_id: str, *, actor: str) -> Dict[str, Any]:
        current = self.get(group_id)
        if current is None:
            raise GroupError(404, f"group {group_id} not found")
        if not current.get("archived_at"):
            return current
        with self._connect() as conn:
            conn.execute(
                "UPDATE collector_groups SET archived_at=NULL, archived_by=NULL, "
                "updated_by=?, updated_at=? WHERE group_id=?",
                (actor, utcnow().isoformat(), group_id),
            )
        return self.get(group_id) or {}

    @staticmethod
    def new_group_id() -> str:
        import secrets

        return f"grp-{secrets.token_hex(4)}"

    # ---------------------------------------------------------------- reads

    def get(self, group_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM collector_groups WHERE group_id=?", (group_id,)
            ).fetchone()
        return self._row(row)

    def find(self, name_or_id: str) -> Optional[Dict[str, Any]]:
        """Resolve a group by id or by name, case-insensitively (CLI convenience)."""
        candidate = (name_or_id or "").strip()
        if not candidate:
            return None
        return self.get(candidate) or self.by_name(candidate)

    def by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM collector_groups WHERE name_key=?", (name.strip().lower(),)
            ).fetchone()
        return self._row(row)

    def list(self, *, include_archived: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM collector_groups"
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        query += " ORDER BY name"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [record for record in (self._row(row) for row in rows) if record]

    def record_resolution(
        self,
        group_id: str,
        agent_ids: Sequence[str],
        *,
        reason: str,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record what this group meant at this moment.

        Membership is never *stored as state* — this is a log of past evaluations, so
        that "it used to be 12 agents" and the shrink guard have something to compare
        against. A drift signal needs a baseline; without one every push looks normal.
        """
        if reason not in self.REASONS:
            raise GroupError(422, f"reason must be one of {', '.join(self.REASONS)}")
        if self.get(group_id) is None:
            raise GroupError(404, f"group {group_id} not found")
        ids = list(dict.fromkeys(str(agent_id) for agent_id in agent_ids))
        now = utcnow().isoformat()
        with self._connect() as conn:
            # `evaluated_at` is the primary key half, so two evaluations in the same
            # microsecond must not collide (a preview loop in a UI does exactly that).
            conn.execute(
                "INSERT OR REPLACE INTO collector_group_resolutions "
                "(group_id, evaluated_at, agent_ids_json, agent_count, reason, actor) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, now, json.dumps(ids), len(ids), reason, actor),
            )
        return {
            "group_id": group_id,
            "evaluated_at": now,
            "agent_ids": ids,
            "agent_count": len(ids),
            "reason": reason,
            "actor": actor,
        }

    def resolutions(self, group_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM collector_group_resolutions WHERE group_id=? "
                "ORDER BY evaluated_at DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        out = []
        for row in rows:
            record = {key: row[key] for key in row.keys()}
            try:
                record["agent_ids"] = json.loads(record.pop("agent_ids_json") or "[]")
            except (TypeError, ValueError):
                record["agent_ids"] = []
            out.append(record)
        return out

    def last_resolution(self, group_id: str, *, before: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The most recent evaluation, optionally one strictly older than `before`.

        `before` is what makes a preview stable: the comparison baseline must not be
        the resolution the preview itself just recorded.
        """
        query = (
            "SELECT * FROM collector_group_resolutions WHERE group_id=?"
        )
        params: List[Any] = [group_id]
        if before:
            query += " AND evaluated_at < ?"
            params.append(before)
        query += " ORDER BY evaluated_at DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        record = {key: row[key] for key in row.keys()}
        try:
            record["agent_ids"] = json.loads(record.pop("agent_ids_json") or "[]")
        except (TypeError, ValueError):
            record["agent_ids"] = []
        return record


def diff_definitions(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Filter-document diff: added / removed / changed keys, with both values.

    Shape is asserted in tests (#111 acceptance: "assert the diff content, not just
    the action name") because the diff is what makes an audit row answer "what did
    this edit change about the blast radius?".
    """
    old = old or {}
    new = new or {}
    added = {key: new[key] for key in new if key not in old}
    removed = {key: old[key] for key in old if key not in new}
    changed = {
        key: {"from": old[key], "to": new[key]}
        for key in new
        if key in old and old[key] != new[key]
    }
    return {"added": added, "removed": removed, "changed": changed}


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

# #77: how many hash-only component reports for one hash may follow a
# `ReportAvailableComponents` ask before the agent is counted as non-compliant. Three
# leaves room for a report that was already in flight when the ask was delivered. The
# counter saturates here so an agent that ignores the flag forever stops changing
# state (and therefore stops causing a row write on every poll).
COMPONENTS_IGNORED_LOG_AT = 3


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
    # #77: the `AvailableComponents.hash` (hex) we last set `ReportAvailableComponents`
    # for. One-shot per hash: an agent that keeps reporting a hash we already asked
    # about is not asked again, and an agent that upgrades (new hash) is asked once
    # more. Persisted so a restart does not repeat the ask.
    last_requested_components_hash: Optional[str] = None
    # #77: hash-only reports received *for that hash* after we asked. Saturates at
    # COMPONENTS_IGNORED_LOG_AT so it stops being a write on every poll; a non-zero
    # value at the threshold is an agent whose implementation ignores the flag.
    components_request_ignored: int = 0
    # #84: a server->agent command waiting to ride the agent's next poll. Persisted,
    # because HTTP has no push and a restart command lost on reboot is a silent no-op.
    # Cleared the moment it is delivered: re-delivering a restart every poll is a
    # crash loop.
    pending_command: Optional[Dict[str, Any]] = None
    command_delivered_at: Optional[str] = None
    # #85: the agent's `CustomCapabilities.capabilities`, replaced wholesale on every
    # report (the spec's message carries the whole list, so appending would resurrect
    # capabilities the agent has dropped).
    custom_capabilities: List[str] = field(default_factory=list)
    # #85: a custom message waiting to ride the agent's next poll. Persisted like
    # `pending_command` (a message lost on restart is a silent no-op) and cleared the
    # moment it is delivered, so it is sent exactly once.
    pending_custom_message: Optional[Dict[str, Any]] = None
    custom_message_delivered_at: Optional[str] = None
    # #98: the sealed body this row was loaded from, kept only when it could not be
    # opened with the keys this process has (see SQLiteAgentStore._sealed_...).
    sealed_effective_config: Optional[str] = field(default=None, repr=False)
    config_error: Optional[str] = None
    # #118: an operator's pin of this agent's type (`otelcol`, `generic`, ...). `None`
    # means "let the server decide", which is the state of every agent nobody has
    # pinned. It is part of the resolution fingerprint, so pinning invalidates the
    # cache without any explicit invalidation call.
    agent_type: Optional[str] = None
    # (fingerprint, Resolution) — resolution and its evidence, re-derived when a
    # report changes the fingerprint (see server/profiles/registry.py).
    _profile_cache: Optional[tuple] = field(default=None, repr=False, compare=False)
    # (the description object, flattened attrs) — the strong reference is what makes
    # `is` comparison safe (see registry.identity_attrs).
    _profile_attrs_cache: Optional[tuple] = field(default=None, repr=False, compare=False)
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
        """Deprecated alias for `server.profiles.otelcol.detect_type` (#118).

        Kept because it was semi-public and the move is meant to be invisible; the
        collector knowledge itself lives in the profile.
        """
        from server.profiles import otelcol

        return otelcol.detect_type(comp_id)

    # -- #118: which profile is this agent? -------------------------------

    def profile_resolution(self) -> "profiles.Resolution":
        """The agent's profile and the evidence for it, memoised on the report."""
        return profiles.resolve_cached(self)

    @property
    def profile(self) -> "profiles.AgentProfile":
        """The profile whose rules apply to this agent right now."""
        return self.profile_resolution().profile

    def type_info(self) -> Dict[str, Any]:
        """The four `agent_type*` fields every read surface shows (#118).

        `agent_type_source` is what makes the value honest: `pinned` is an operator's
        decision, `declared`/`sniffed` are the agent's own claims or evidence, and
        `fallback` means nothing matched. `agent_type_warning` is non-null exactly
        when the server is **not** interpreting this agent's config, which is the
        thing an operator needs to know before pushing one.
        """
        resolution = self.profile_resolution()
        profile = resolution.profile
        return {
            "agent_type": profile.name,
            "agent_type_source": resolution.source,
            "agent_type_pinned": self.agent_type,
            "agent_type_warning": (
                None
                if profile.validates_structure
                else (
                    f"this agent resolves to the {profile.name!r} profile, so the "
                    "server does not interpret its configuration: collector-specific "
                    "checks, reports and telemetry views do not describe it"
                )
            ),
        }

    @property
    def components(self) -> dict:
        """Component inventory, as the agent's profile defines it (#118).

        `{}` for a profile that has no such concept — which is the honest answer, not
        an empty collector.
        """
        return self.profile.extract_components(self)

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

    def matches_scope(self, filters: Optional[Dict[str, Any]]) -> bool:
        """`matches_filters` for a raw filter document (#111).

        This is the **one** filter implementation: `GET /agents`, config selector
        scopes and collector groups all arrive as a document like
        ``{"healthy": "true", "environment": "prod"}`` and every one of them has to
        answer the same way. Groups exist because a saved selector must mean exactly
        what the ad-hoc filter it replaces meant, so the split between reserved keys
        (`healthy`, `status`, `remote_config_status`) and description attributes
        lives here rather than in each caller.
        """
        filters = filters or {}
        healthy = filters.get("healthy")
        remote_status = filters.get("remote_config_status") or filters.get("status")
        attributes: Dict[str, List[str]] = {}
        for key, value in filters.items():
            if key in RESERVED_FILTER_KEYS:
                continue
            values = value if isinstance(value, (list, tuple)) else [value]
            attributes[str(key)] = [str(item) for item in values]
        return self.matches_filters(
            healthy=None if healthy is None else str(healthy),
            remote_config_status=None if remote_status is None else str(remote_status),
            attributes=attributes,
        )

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
            # #85: "what does this collector support beyond the spec, and is a custom
            # message queued for it?" belongs in the agent view, not only in a log.
            "custom_capabilities": list(self.custom_capabilities or []),
            "pending_custom_message": self.pending_custom_message,
            "custom_message_delivered_at": self.custom_message_delivered_at,
            # #75: `None` for every agent whose uid was never re-issued; otherwise the
            # uid the agent presented and the signal that convicted it.
            "uid_reissued_from": self.uid_reissued_from,
            "uid_reissue_reason": self.uid_reissue_reason,
            # #77: `last_requested_components_hash` explains why the server asked for
            # full components; a non-zero `components_request_ignored` is an agent that
            # has not answered an ask it was given.
            "last_requested_components_hash": self.last_requested_components_hash,
            "components_request_ignored": self.components_request_ignored,
            "package_statuses": self.package_statuses,
            "available_components": self.available_components,
            "components": self.components,
            "compliance": self.compliance,
            # #118: what the server believes this agent is, how it decided, and the
            # operator's pin if there is one. `agent_type_pinned` is kept separate from
            # `agent_type` because they differ on every auto-detected agent, and "pinned
            # to generic" and "fell back to generic" are different facts. `offline
            # opampctl --db` inherits all four for free.
            **self.type_info(),
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
# #111: named collector groups. Module-level like AGENT_REGISTRY so the config control
# plane and the routes read the same store.
COLLECTOR_GROUPS = SQLiteGroupStore()
