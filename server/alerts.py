import os
import json
import sqlite3
import uuid
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from loguru import logger

from server.state import DB_PATH

ALERT_EVENT_NEW_AGENT = "new_agent"
ALERT_EVENT_AGENT_DISCONNECTED = "agent_disconnected"
ALERT_EVENT_COMPLIANCE_VIOLATION = "compliance_violation"

ALERT_EVENTS = [
    ALERT_EVENT_NEW_AGENT,
    ALERT_EVENT_AGENT_DISCONNECTED,
    ALERT_EVENT_COMPLIANCE_VIOLATION,
]

ALERT_TYPE_WEBHOOK = "webhook"

ALERT_TYPES = [
    ALERT_TYPE_WEBHOOK,
]

DEFAULT_EVENT_CONFIG = {
    "enabled": False,
    "webhook_url": "",
    "headers": "",
    "body_template": "",
}

ALERT_CONFIG = {
    "events": {
        ALERT_EVENT_NEW_AGENT: DEFAULT_EVENT_CONFIG.copy(),
        ALERT_EVENT_AGENT_DISCONNECTED: DEFAULT_EVENT_CONFIG.copy(),
        ALERT_EVENT_COMPLIANCE_VIOLATION: DEFAULT_EVENT_CONFIG.copy(),
    },
}

CLOUDEVENTS_BODY_TEMPLATE = json.dumps({
    "specversion": "1.0",
    "type": "io.opentelemetry.opamp.agent.{event_type}",
    "source": "opamp-server",
    "id": "{id}",
    "time": "{time}",
    "datacontenttype": "application/json",
    "data": {
        "message": "{message}",
    },
})


def get_alert_config():
    return ALERT_CONFIG.copy()


def _init_alert_config_table(conn: sqlite3.Connection):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alert_config ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), config TEXT NOT NULL)"
    )


def load_alert_config_from_db(db_path: Path = DB_PATH) -> None:
    """Load the persisted alert config from SQLite into ALERT_CONFIG (best-effort).

    Defensive against stale/foreign data: unknown events are dropped, missing keys
    fall back to DEFAULT_EVENT_CONFIG, and corrupt JSON or an unavailable DB just
    logs a warning and keeps the in-memory defaults.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            _init_alert_config_table(conn)
            row = conn.execute(
                "SELECT config FROM alert_config WHERE id=1"
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning(f"Could not read alert config from {db_path}: {exc}")
        return

    if not row:
        return
    try:
        stored = json.loads(row[0])
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(f"Corrupt persisted alert config, using defaults: {exc}")
        return

    for event, event_config in (stored.get("events") or {}).items():
        if event not in ALERT_CONFIG["events"]:
            continue
        merged = DEFAULT_EVENT_CONFIG.copy()
        merged.update(event_config or {})
        ALERT_CONFIG["events"][event] = merged
    logger.info(f"Loaded alert config from {db_path}")


def save_alert_config_to_db(db_path: Path = None) -> None:
    """Persist ALERT_CONFIG to SQLite (best-effort; warns on failure)."""
    db_path = db_path or DB_PATH
    try:
        with sqlite3.connect(db_path) as conn:
            _init_alert_config_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO alert_config (id, config) VALUES (1, ?)",
                (json.dumps(ALERT_CONFIG),),
            )
    except sqlite3.Error as exc:
        logger.warning(f"Could not persist alert config to {db_path}: {exc}")


load_alert_config_from_db()


def update_alert_config(config: dict):
    if "events" in config:
        for event, event_config in config["events"].items():
            if event in ALERT_CONFIG["events"]:
                ALERT_CONFIG["events"][event].update(event_config)
    save_alert_config_to_db()
    return get_alert_config()


def _send_webhook(message: str, config: dict, event_type: str):
    url = config.get("webhook_url", "")
    if not url:
        return False, "webhook_url not configured"
    
    body_template = config.get("body_template", "")
    headers_str = config.get("headers", "")
    
    if not body_template:
        body_template = CLOUDEVENTS_BODY_TEMPLATE
    
    replacements = {
        "{event_type}": event_type,
        "{message}": message,
        "{id}": str(uuid.uuid4()),
        "{time}": datetime.now(timezone.utc).isoformat(),
    }
    
    body = body_template
    for placeholder, value in replacements.items():
        body = body.replace(placeholder, value)
    
    headers = {"Content-Type": "application/cloudevents+json; charset=UTF-8"}
    
    if headers_str:
        try:
            custom_headers = json.loads(headers_str)
            headers.update(custom_headers)
        except json.JSONDecodeError:
            pass
    
    try:
        resp = requests.post(url, data=body.encode(), headers=headers, timeout=10)
        return resp.status_code < 400, f"status: {resp.status_code}"
    except Exception as e:
        return False, str(e)


DISPATCHERS = {
    ALERT_TYPE_WEBHOOK: _send_webhook,
}


def send_alert(message: str, event_type: str = ALERT_EVENT_NEW_AGENT) -> tuple[bool, str]:
    event_config = ALERT_CONFIG.get("events", {}).get(event_type, {})
    
    if not event_config.get("enabled", False):
        return False, f"{event_type} disabled"
    
    dispatcher = DISPATCHERS.get(ALERT_TYPE_WEBHOOK)
    
    if not dispatcher:
        return False, "no dispatcher"
    
    logger.info(f"Sending {event_type} alert: {message}")
    return dispatcher(message, event_config, event_type)


def send_new_agent_alert(agent_id: str) -> tuple[bool, str]:
    return send_alert(f"**New agent connected:** {agent_id}", ALERT_EVENT_NEW_AGENT)


def send_stale_agent_alert(agent_id: str) -> tuple[bool, str]:
    return send_alert(f"**Agent disconnected:** {agent_id}", ALERT_EVENT_AGENT_DISCONNECTED)


def send_compliance_alert(agent_id: str, violations: list) -> tuple[bool, str]:
    violations_text = ", ".join(violations)
    return send_alert(f"**Compliance violation:** {agent_id} - {violations_text}", ALERT_EVENT_COMPLIANCE_VIOLATION)


def send_test_alert(event_type: str = ALERT_EVENT_NEW_AGENT) -> tuple[bool, str]:
    return send_alert(f"Test alert for {event_type}", event_type)