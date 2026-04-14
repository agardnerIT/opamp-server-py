import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, Set
import yaml
from loguru import logger


def utcnow():
    return datetime.now(timezone.utc)


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
