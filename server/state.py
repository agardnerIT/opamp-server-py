import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Set
import yaml
from loguru import logger


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
                    updated_at TEXT
                )
            """)
            
            cursor = conn.execute("PRAGMA table_info(agents)")
            columns = [row[1] for row in cursor.fetchall()]
            if "compliance" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN compliance TEXT")

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
                                   health, compliance, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    updated_at=excluded.updated_at
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
            state.effective_config,
            state.remote_config_status,
            json.dumps(state.package_statuses),
            json.dumps(state.available_components),
            json.dumps(state.health),
            json.dumps(state.compliance) if state.compliance else None,
            utcnow().isoformat(),
        )

    def _row_to_state(self, row) -> "AgentState":
        return AgentState(
            instance_uid=row["instance_uid"],
            agent_id=row["agent_id"],
            connected_at=_parse_ts(row["connected_at"]),
            last_heartbeat=_parse_ts(row["last_heartbeat"]),
            healthy=bool(row["healthy"]) if row["healthy"] is not None else None,
            capabilities=row["capabilities"] or 0,
            description=json.loads(row["description"] or "{}"),
            effective_config=row["effective_config"],
            remote_config_status=row["remote_config_status"] or "UNSET",
            package_statuses=json.loads(row["package_statuses"] or "{}"),
            available_components=json.loads(row["available_components"] or "{}"),
            health=json.loads(row["health"] or "{}"),
            compliance=json.loads(row["compliance"] or "null"),
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
            logger.warning(f"Could not init metrics store at {db_path}: {exc}")

    def upsert(self, agent_id: str, entry: Dict[str, Any]) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO agent_metrics (agent_id, metrics, updated_at) "
                    "VALUES (?, ?, ?)",
                    (agent_id, json.dumps(entry.get("metrics", {})), entry.get("updated_at") or utcnow().isoformat()),
                )
        except sqlite3.Error as exc:
            logger.warning(f"Could not persist metrics for {agent_id}: {exc}")

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT metrics, updated_at FROM agent_metrics WHERE agent_id=?", (agent_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning(f"Could not read metrics for {agent_id}: {exc}")
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
            logger.warning(f"Could not delete metrics for {agent_id}: {exc}")

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


def parse_effective_config(effective_config: Dict[str, Any]) -> Dict[str, set]:
    try:
        import base64
        config_map = effective_config.get("configMap", {}).get("configMap", {})
        key = ""
        body = config_map.get(key, {}).get("body", b"")
        
        if isinstance(body, str):
            try:
                body = base64.b64decode(body).decode("utf-8")
            except Exception:
                body = body.encode("utf-8").decode("utf-8")
        elif isinstance(body, bytes):
            body = body.decode("utf-8")
        if not body:
            return {}
        
        parsed = yaml.safe_load(body)
        if not parsed:
            return {}
        
        used_by_type = defaultdict(set)
        service = parsed.get("service", {})
        
        extensions = service.get("extensions", [])
        if isinstance(extensions, list):
            for ext in extensions:
                used_by_type["extension"].add(ext)
        
        pipelines = service.get("pipelines", {})
        for pipeline in pipelines.values():
            for receiver in pipeline.get("receivers", []):
                used_by_type["receiver"].add(receiver)
            for processor in pipeline.get("processors", []):
                used_by_type["processor"].add(processor)
            for exporter in pipeline.get("exporters", []):
                used_by_type["exporter"].add(exporter)
        
        return dict(used_by_type)
    except Exception:
        return {}

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
        
        used_by_type = {}
        
        if self.effective_config:
            try:
                import json
                eff_config = json.loads(self.effective_config)
                used_by_type = parse_effective_config(eff_config)
            except Exception as e:
                logger.warning(f"Failed to parse effective_config: {e}")
                used_by_type = {}
        
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.agent_id,
            "healthy": self.healthy,
            "connected_at": self.connected_at.isoformat(),
            "last_heartbeat": self.last_heartbeat.isoformat(),
            "capabilities": self.capabilities,
            "capability_tags": self.capability_tags,
            "description": self.description,
            "effective_config": self.effective_config,
            "remote_config_status": self.remote_config_status,
            "package_statuses": self.package_statuses,
            "available_components": self.available_components,
            "components": self.components,
            "compliance": self.compliance,
        }


class AgentRegistry:
    def __init__(self):
        self._store = SQLiteAgentStore()
        self._agents: Dict[str, AgentState] = self._store.load_all()

    def register(self, agent_id: str, state: AgentState) -> None:
        self._agents[agent_id] = state
        self._store.upsert(state)

    def get(self, agent_id: str) -> Optional[AgentState]:
        return self._agents.get(agent_id)

    def update(self, agent_id: str, **kwargs) -> None:
        if agent_id in self._agents:
            for key, value in kwargs.items():
                setattr(self._agents[agent_id], key, value)
            self._agents[agent_id].last_heartbeat = utcnow()
            self._store.upsert(self._agents[agent_id])

    def remove(self, agent_id: str) -> bool:
        if agent_id in self._agents:
            del self._agents[agent_id]
            self._store.remove(agent_id)
            return True
        return False

    def list_all(self) -> list:
        return list(self._agents.values())

    @property
    def count(self) -> int:
        return len(self._agents)


AGENT_REGISTRY = AgentRegistry()
