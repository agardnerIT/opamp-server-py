"""opamp_client — shared HTTP client for the OpAMP server REST API.

One client used by the CLI (opampctl), the MCP server, and (optionally) the
Streamlit UI, so HTTP/auth/error handling is implemented exactly once.

Design rules (from the AI-accessibility epic, issue #54):
- httpx-based, JSON everywhere (send JSON, parse JSON, raise structured errors)
- Basic auth (matches the server's ADMIN_PASSWORD mechanism; no new auth system)
- Typed methods mirroring the documented API 1:1 — agents never guess shapes
- Server URL from the ``OPAMP_SERVER_URL`` env var, overridable per-instance
- Admin password from the ``ADMIN_PASSWORD`` env var (or passed explicitly)

Example::

    from client.opamp_client import OpampClient

    oc = OpampClient()                       # reads OPAMP_SERVER_URL / ADMIN_PASSWORD
    print(oc.health())
    agents = oc.list_agents(healthy="true")
    result = oc.generate_manifest(agents["agents"][0]["id"])
    print(result["manifest_yaml"])

Errors are structured: any non-2xx response raises :class:`OpampApiError`
with ``.status_code`` and ``.detail`` (the FastAPI ``detail`` field when
present, the raw body text otherwise).
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

DEFAULT_SERVER_URL = "http://localhost:4320"
DEFAULT_TIMEOUT = 30.0


class OpampClientError(Exception):
    """Base class for all opamp_client errors."""


class OpampConnectionError(OpampClientError):
    """Could not reach the server (DNS, connection refused, timeout, ...)."""


class OpampApiError(OpampClientError):
    """The server responded with a non-2xx status.

    Attributes:
        status_code: HTTP status code of the response.
        detail: The server's ``detail`` field (FastAPI error shape) when
            present, else the raw response body text.
    """

    def __init__(self, status_code: int, detail: Any, url: str = ""):
        self.status_code = status_code
        self.detail = detail
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}: {detail}")


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_SERVER_URL
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise OpampClientError(f"Unsupported server URL scheme: {parsed.scheme!r} in {url!r}")
    return url


class OpampClient:
    """HTTP client for the OpAMP server. All methods return parsed JSON (dicts).

    Args:
        base_url: Server base URL. Falls back to ``OPAMP_SERVER_URL`` env var,
            then ``http://localhost:4320``.
        password: Admin password for Basic auth on admin endpoints. Falls back
            to the ``ADMIN_PASSWORD`` env var. Sent only when set.
        timeout: Per-request timeout in seconds.
        transport: Optional httpx transport (for tests / custom routing).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.base_url = _normalize_base_url(base_url or os.environ.get("OPAMP_SERVER_URL"))
        self.password = password if password is not None else os.environ.get("ADMIN_PASSWORD") or ""
        self.timeout = timeout
        headers = {"Accept": "application/json"}
        if self.password:
            token = base64.b64encode(f":{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------ core

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform a request, returning parsed JSON. Raises structured errors."""
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OpampConnectionError(f"Cannot reach {self.base_url}: {exc}") from exc
        if resp.status_code < 200 or resp.status_code >= 300:
            try:
                body: Any = resp.json()
                detail = body.get("detail", body) if isinstance(body, dict) else body
            except ValueError:
                detail = resp.text
            raise OpampApiError(resp.status_code, detail, url=str(resp.request.url))
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, json: Any = None) -> Any:
        return self._request("POST", path, json=json)

    def _put(self, path: str, json: Any = None) -> Any:
        return self._request("PUT", path, json=json)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpampClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ ops

    def health(self) -> Dict[str, Any]:
        """``GET /health`` — status, agent count, OPA availability."""
        return self._get("/health")

    # ------------------------------------------------------------------ auth

    def auth_status(self) -> Dict[str, Any]:
        """``GET /auth/status`` — whether admin auth is required."""
        return self._get("/auth/status")

    def auth_verify(self, password: str) -> Dict[str, Any]:
        """``GET /auth/verify`` — verify a password (sent for this call only)."""
        token = base64.b64encode(f":{password}".encode()).decode()
        try:
            resp = self._client.get("/auth/verify", headers={"Authorization": f"Basic {token}"})
        except httpx.HTTPError as exc:
            raise OpampConnectionError(f"Cannot reach {self.base_url}: {exc}") from exc
        if resp.status_code == 200:
            return {"verified": True}
        if resp.status_code == 401:
            return {"verified": False}
        try:
            body: Any = resp.json()
            detail = body.get("detail", body) if isinstance(body, dict) else body
        except ValueError:
            detail = resp.text
        raise OpampApiError(resp.status_code, detail, url=str(resp.request.url))

    # ------------------------------------------------------------------ agents

    def list_agents(
        self,
        healthy: Optional[str] = None,
        status: Optional[str] = None,
        remote_config_status: Optional[str] = None,
        **attributes: Any,
    ) -> Dict[str, Any]:
        """``GET /agents`` — list agents, optionally filtered.

        Args:
            healthy: ``"true"``, ``"false"`` or ``"unknown"``.
            status: Remote config status filter (UNSET/APPLIED/APPLYING/FAILED).
            remote_config_status: Explicit alias for ``status``.
            **attributes: Any other keyword filters on agent description
                metadata, e.g. ``environment="prod"`` (repeated values pass a
                list: ``environment=["prod", "staging"]``).
        """
        params: Dict[str, Any] = {}
        if healthy is not None:
            params["healthy"] = healthy
        if remote_config_status is not None:
            params["remote_config_status"] = remote_config_status
        elif status is not None:
            params["status"] = status
        for key, value in attributes.items():
            params[key] = value if isinstance(value, list) else [value]
        return self._get("/agents", params=params or None)

    def get_agent(self, agent_id: str) -> Dict[str, Any]:
        """``GET /agent/{id}`` — full details for one agent (incl. metrics)."""
        return self._get(f"/agent/{agent_id}")

    def get_agent_metrics(self, agent_id: str) -> Dict[str, Any]:
        """``GET /agent/{id}/metrics`` — latest ingested OTLP metric values."""
        return self._get(f"/agent/{agent_id}/metrics")

    # ------------------------------------------------------------------ manifests

    def generate_manifest(self, agent_id: str, version: Optional[str] = None) -> Dict[str, Any]:
        """``POST /agent/{id}/manifest`` — OCB manifest.yaml for a slim build.

        Returns ``{"manifest_yaml", "ocb_command", "collector_version"}``.
        Raises 409-shaped OpampApiError if the agent has no components in use,
        422 if ``version`` is not semver.
        """
        body = {"version": version} if version is not None else None
        return self._post(f"/agent/{agent_id}/manifest", json=body)

    # ------------------------------------------------------------------ reports

    def agent_report(self, agent_id: str) -> Dict[str, Any]:
        """``GET /agent/{id}/report`` — markdown report for one agent."""
        return self._get(f"/agent/{agent_id}/report")

    def fleet_report(self) -> Dict[str, Any]:
        """``GET /reports/fleet`` — fleet-wide markdown report."""
        return self._get("/reports/fleet")

    def heavy_collectors_report(self, threshold: float = 0.5) -> Dict[str, Any]:
        """``GET /reports/heavy-collectors`` — collectors with many unused components."""
        return self._get("/reports/heavy-collectors", params={"threshold": threshold})

    def outdated_collectors_report(self, version: str = "0.149.0") -> Dict[str, Any]:
        """``GET /reports/outdated-collectors`` — collectors with components older than ``version``."""
        return self._get("/reports/outdated-collectors", params={"version": version})

    # ------------------------------------------------------------------ compliance

    def get_compliance(self, agent_id: str) -> Dict[str, Any]:
        """``GET /agent/{id}/compliance`` — evaluate one agent against OPA policies."""
        return self._get(f"/agent/{agent_id}/compliance")

    def check_compliance(self, agent_id: str) -> Dict[str, Any]:
        """``POST /compliance/check/{id}`` — force a compliance check (admin)."""
        return self._post(f"/compliance/check/{agent_id}")

    def compliance_summary(self) -> Dict[str, Any]:
        """``GET /compliance/summary`` — fleet-wide compliance counts."""
        return self._get("/compliance/summary")

    def list_policies(self) -> Dict[str, Any]:
        """``GET /compliance/policies`` — available OPA policies."""
        return self._get("/compliance/policies")

    def reload_policies(self) -> Dict[str, Any]:
        """``POST /compliance/reload`` — ask OPA to reload policies (admin)."""
        return self._post("/compliance/reload")

    def validate_policies(self) -> Dict[str, Any]:
        """``GET /compliance/validate`` — validate OPA policy files."""
        return self._get("/compliance/validate")

    # ------------------------------------------------------------------ alerts

    def get_alerts(self) -> Dict[str, Any]:
        """``GET /alerts`` — alert configuration (admin)."""
        return self._get("/alerts")

    def update_alerts(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """``PUT /alerts`` — replace/merge alert configuration (admin).

        Pass the same shape ``GET /alerts`` returns under ``config``.
        """
        return self._put("/alerts", json=config)

    def test_alerts(
        self,
        event_type: str = "new_agent",
        event_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``POST /alerts/test`` — fire a test alert through the dispatcher (admin).

        ``event_config`` (when given) is used for this one send and reverted.
        """
        body: Dict[str, Any] = {"event_type": event_type}
        if event_config is not None:
            body["event_config"] = event_config
        return self._post("/alerts/test", json=body)
