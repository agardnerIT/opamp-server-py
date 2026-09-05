"""Tests for alert config SQLite persistence (issue #50, gap G3)."""

import json
import sqlite3

from server import alerts


def _read_row(db_path):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT config FROM alert_config WHERE id=1").fetchone()
    return json.loads(row[0]) if row else None


def _reset_config():
    alerts.ALERT_CONFIG["events"] = {
        event: alerts.DEFAULT_EVENT_CONFIG.copy() for event in alerts.ALERT_EVENTS
    }


def test_update_persists_to_sqlite(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(alerts, "DB_PATH", db)
    _reset_config()
    alerts.update_alert_config(
        {"events": {"new_agent": {"enabled": True, "webhook_url": "http://x/y"}}}
    )
    stored = _read_row(db)
    assert stored["events"]["new_agent"]["enabled"] is True
    assert stored["events"]["new_agent"]["webhook_url"] == "http://x/y"


def test_load_restores_after_reset(tmp_path):
    db = tmp_path / "test.db"
    _reset_config()
    alerts.ALERT_CONFIG["events"]["compliance_violation"]["enabled"] = True
    alerts.ALERT_CONFIG["events"]["compliance_violation"]["webhook_url"] = "http://w"
    alerts.save_alert_config_to_db(db)

    _reset_config()
    assert alerts.ALERT_CONFIG["events"]["compliance_violation"]["enabled"] is False

    alerts.load_alert_config_from_db(db)
    assert alerts.ALERT_CONFIG["events"]["compliance_violation"]["enabled"] is True
    assert alerts.ALERT_CONFIG["events"]["compliance_violation"]["webhook_url"] == "http://w"
    # untouched event keeps defaults
    assert alerts.ALERT_CONFIG["events"]["new_agent"] == alerts.DEFAULT_EVENT_CONFIG


def test_load_defensive_merge(tmp_path):
    db = tmp_path / "test.db"
    stored = {
        "events": {
            "new_agent": {"enabled": True, "headers": "X-Foo: bar"},
            "future_event": {"enabled": True},  # unknown event -> dropped
            "agent_disconnected": None,  # null config -> all defaults
        }
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE alert_config (id INTEGER PRIMARY KEY CHECK (id = 1), config TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO alert_config (id, config) VALUES (1, ?)", (json.dumps(stored),)
        )

    _reset_config()
    alerts.load_alert_config_from_db(db)

    na = alerts.ALERT_CONFIG["events"]["new_agent"]
    assert na["enabled"] is True
    assert na["headers"] == "X-Foo: bar"
    assert na["webhook_url"] == ""  # missing key falls back to default
    assert "future_event" not in alerts.ALERT_CONFIG["events"]
    assert alerts.ALERT_CONFIG["events"]["agent_disconnected"] == alerts.DEFAULT_EVENT_CONFIG


def test_load_corrupt_json_keeps_defaults(tmp_path):
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE alert_config (id INTEGER PRIMARY KEY CHECK (id = 1), config TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO alert_config (id, config) VALUES (1, '{not json')")

    _reset_config()
    alerts.load_alert_config_from_db(db)
    assert alerts.ALERT_CONFIG["events"]["new_agent"] == alerts.DEFAULT_EVENT_CONFIG


def test_load_missing_table_is_noop(tmp_path):
    _reset_config()
    # no alert_config table at all -> no crash, no change
    alerts.load_alert_config_from_db(tmp_path / "empty.db")
    assert alerts.ALERT_CONFIG["events"]["new_agent"] == alerts.DEFAULT_EVENT_CONFIG


def test_save_unavailable_db_warns_not_raises(tmp_path):
    _reset_config()
    # a directory path makes sqlite3 fail on execute -> swallowed with a warning
    alerts.save_alert_config_to_db(tmp_path)


def test_webhook_is_only_dispatcher():
    assert list(alerts.DISPATCHERS) == [alerts.ALERT_TYPE_WEBHOOK]
