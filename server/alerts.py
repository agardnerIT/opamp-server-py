import os
import json
import uuid
import requests
from datetime import datetime, timezone
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


def update_alert_config(config: dict):
    if "events" in config:
        for event, event_config in config["events"].items():
            if event in ALERT_CONFIG["events"]:
                ALERT_CONFIG["events"][event].update(event_config)
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