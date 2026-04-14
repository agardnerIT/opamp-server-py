import os
import json
import requests
from typing import Optional
from loguru import logger

ALERT_EVENT_NEW_AGENT = "new_agent"
ALERT_EVENT_AGENT_DISCONNECTED = "agent_disconnected"
ALERT_EVENT_COMPLIANCE_VIOLATION = "compliance_violation"

ALERT_EVENTS = [
    ALERT_EVENT_NEW_AGENT,
    ALERT_EVENT_AGENT_DISCONNECTED,
    ALERT_EVENT_COMPLIANCE_VIOLATION,
]

ALERT_TYPE_WEBHOOK = "webhook"
ALERT_TYPE_SLACK = "slack"
ALERT_TYPE_DISCORD = "discord"
ALERT_TYPE_CLOUDEVENTS = "cloudEvents"
ALERT_TYPE_TELEGRAM = "telegram"

ALERT_TYPES = [
    ALERT_TYPE_WEBHOOK,
    ALERT_TYPE_SLACK,
    ALERT_TYPE_DISCORD,
    ALERT_TYPE_CLOUDEVENTS,
    ALERT_TYPE_TELEGRAM,
]

DEFAULT_EVENT_CONFIG = {
    "enabled": False,
    "type": ALERT_TYPE_WEBHOOK,
    "webhook_url": "",
    "headers": "{}",
    "body_template": '{"text": "{message}"}',
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}

ALERT_CONFIG = {
    "enabled": os.environ.get("ALERT_ENABLED", "false").lower() == "true",
    "events": {
        ALERT_EVENT_NEW_AGENT: DEFAULT_EVENT_CONFIG.copy(),
        ALERT_EVENT_AGENT_DISCONNECTED: DEFAULT_EVENT_CONFIG.copy(),
        ALERT_EVENT_COMPLIANCE_VIOLATION: DEFAULT_EVENT_CONFIG.copy(),
    },
}


def get_alert_config():
    return ALERT_CONFIG.copy()


def update_alert_config(config: dict):
    if "enabled" in config:
        ALERT_CONFIG["enabled"] = config["enabled"]
    if "events" in config:
        for event, event_config in config["events"].items():
            if event in ALERT_CONFIG["events"]:
                ALERT_CONFIG["events"][event].update(event_config)
    return get_alert_config()


def _send_webhook(message: str, config: dict):
    url = config.get("webhook_url", "")
    if not url:
        return False, "webhook_url not configured"
    
    headers = json.loads(config.get("headers", "{}"))
    body = config.get("body_template", '{"text": "{message}"}').format(message=message)
    
    try:
        resp = requests.post(url, data=body.encode(), headers={**headers, "Content-Type": "application/json"}, timeout=10)
        return resp.status_code < 400, f"status: {resp.status_code}"
    except Exception as e:
        return False, str(e)


def _send_slack(message: str, config: dict):
    url = config.get("webhook_url", "")
    if not url:
        return False, "webhook_url not configured"
    
    payload = {"text": message}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code < 400, f"status: {resp.status_code}"
    except Exception as e:
        return False, str(e)


def _send_discord(message: str, config: dict):
    url = config.get("webhook_url", "")
    if not url:
        return False, "webhook_url not configured"
    
    payload = {"content": message}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code < 400, f"status: {resp.status_code}"
    except Exception as e:
        return False, str(e)


def _send_cloudevents(message: str, config: dict):
    url = config.get("webhook_url", "")
    if not url:
        return False, "webhook_url not configured"
    
    headers = json.loads(config.get("headers", "{}"))
    body = config.get("body_template", '{"text": "{message}"}').format(message=message)
    
    ce_headers = {
        "Content-Type": "application/cloudevents+json",
        "ce-type": os.environ.get("CDEVENT_TYPE", "opamp.agent.event"),
        "ce-source": "opamp-server",
    }
    
    try:
        resp = requests.post(url, data=body.encode(), headers={**headers, **ce_headers}, timeout=10)
        return resp.status_code < 400, f"status: {resp.status_code}"
    except Exception as e:
        return False, str(e)


def _send_telegram(message: str, config: dict):
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")
    
    if not token or not chat_id:
        return False, "telegram_bot_token or telegram_chat_id not configured"
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code < 400, f"status: {resp.status_code}"
    except Exception as e:
        return False, str(e)


DISPATCHERS = {
    ALERT_TYPE_WEBHOOK: _send_webhook,
    ALERT_TYPE_SLACK: _send_slack,
    ALERT_TYPE_DISCORD: _send_discord,
    ALERT_TYPE_CLOUDEVENTS: _send_cloudevents,
    ALERT_TYPE_TELEGRAM: _send_telegram,
}


def send_alert(message: str, event_type: str = ALERT_EVENT_NEW_AGENT) -> tuple[bool, str]:
    if not ALERT_CONFIG.get("enabled", False):
        logger.warning("Alerts disabled globally")
        return False, "alerts disabled"
    
    event_config = ALERT_CONFIG.get("events", {}).get(event_type, {})
    
    if not event_config.get("enabled", False):
        logger.warning(f"Event {event_type} disabled")
        return False, f"{event_type} disabled"
    
    alert_type = event_config.get("type", ALERT_TYPE_WEBHOOK)
    dispatcher = DISPATCHERS.get(alert_type)
    
    if not dispatcher:
        return False, f"unknown alert type: {alert_type}"
    
    logger.info(f"Sending {event_type} alert via {alert_type}: {message}")
    return dispatcher(message, event_config)


def send_new_agent_alert(agent_id: str) -> tuple[bool, str]:
    return send_alert(f"**New agent connected:** {agent_id}", ALERT_EVENT_NEW_AGENT)


def send_stale_agent_alert(agent_id: str) -> tuple[bool, str]:
    return send_alert(f"**Agent disconnected:** {agent_id}", ALERT_EVENT_AGENT_DISCONNECTED)


def send_compliance_alert(agent_id: str, violations: list) -> tuple[bool, str]:
    violations_text = ", ".join(violations)
    return send_alert(f"**Compliance violation:** {agent_id} - {violations_text}", ALERT_EVENT_COMPLIANCE_VIOLATION)


def send_test_alert(event_type: str = ALERT_EVENT_NEW_AGENT) -> tuple[bool, str]:
    return send_alert(f"Test alert for {event_type}", event_type)