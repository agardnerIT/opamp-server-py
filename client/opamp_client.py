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
from typing import Any, Dict, List, Optional
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


def _drop_empty(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drop unset query parameters (``None`` means "do not filter", not "empty")."""
    return {key: value for key, value in params.items() if value is not None}


class OpampClient:
    """HTTP client for the OpAMP server. All methods return parsed JSON (dicts).

    Args:
        base_url: Server base URL. Falls back to ``OPAMP_SERVER_URL`` env var,
            then ``http://localhost:4320``.
        password: Admin password for Basic auth on privileged endpoints. Falls
            back to the ``ADMIN_PASSWORD`` env var. Sent only when set;
            deprecated for interactive use (#95) but still what scripts use.
        token: Optional session token (``POST /auth/session``). Falls back to the
            ``OPAMP_ADMIN_TOKEN`` env var. When both are given the token wins —
            it carries a role and an identity, and it can be revoked.
        timeout: Per-request timeout in seconds.
        transport: Optional httpx transport (for tests / custom routing).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[httpx.BaseTransport] = None,
        token: Optional[str] = None,
    ):
        self.base_url = _normalize_base_url(base_url or os.environ.get("OPAMP_SERVER_URL"))
        self.password = password if password is not None else os.environ.get("ADMIN_PASSWORD") or ""
        self.token = token if token is not None else os.environ.get("OPAMP_ADMIN_TOKEN") or ""
        self.timeout = timeout
        headers = {"Accept": "application/json"}
        if self.token:
            # A session token beats the shared password: it carries an identity and
            # a role, and it can be revoked (#95).
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.password:
            basic = base64.b64encode(f":{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------ core

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Perform a request and map transport/status failures to client errors."""
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
        return resp

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform a request, returning parsed JSON. Raises structured errors."""
        resp = self._send(method, path, **kwargs)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _request_text(self, method: str, path: str, **kwargs: Any) -> str:
        """Return the raw body.

        Exports are `application/x-ndjson` or `text/csv`, and a one-row JSONL export
        is also valid JSON — sniffing the body would turn a log line into a dict, so
        the exporter asks for text explicitly.
        """
        return self._send(method, path, **kwargs).text

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(
        self, path: str, json: Any = None, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        return self._request("POST", path, json=json, params=params)

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
        """``GET /health`` — status, agent count, OPA availability, posture."""
        return self._get("/health")

    # ------------------------------------------------------------- updates

    def get_updates(self) -> Dict[str, Any]:
        """``GET /updates`` — cached update-check state. Never triggers a check."""
        return self._get("/updates")

    def check_updates(self) -> Dict[str, Any]:
        """``POST /updates/check`` — run one check now (admin; contacts GitHub)."""
        return self._post("/updates/check")

    def set_update_check(self, enabled: Optional[bool]) -> Dict[str, Any]:
        """``PUT /updates/settings`` — admin override; ``None`` clears it."""
        return self._put("/updates/settings", json={"enabled": enabled})

    def config_posture(self) -> Dict[str, Any]:
        """``GET /config/posture`` — config push security posture (viewer).

        Reports every requirement (admin auth, agent auth, TLS), which are met,
        where the admin credential came from and whether the development override
        is in force. Never contains a secret. 401 without valid credentials when
        an admin credential is configured.
        """
        return self._get("/config/posture")

    # ------------------------------------------------------------------ auth

    def auth_status(self) -> Dict[str, Any]:
        """``GET /auth/status`` — how to authenticate (no secrets).

        ``{"password_required", "auth_mode": "password"|"principals"|"disabled",
        "roles", "sessions", "principals", "legacy_password"}``. ``disabled`` means
        read-only mode: reads are open and privileged writes answer 403.
        """
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


    # ------------------------------------------------- identities and sessions

    def login(self, name: str = "", password: str = "") -> Dict[str, Any]:
        """``POST /auth/session`` — exchange name+password for a bearer token.

        Returns ``{"token", "token_type", "expires_at", "principal": {"name",
        "role"}, "ttl_seconds", "idle_timeout_seconds"}``. The token is shown once
        and stored only as a SHA-256 hash. Pass it as ``token=`` to this client (or
        ``Authorization: Bearer``) for later calls. With only the legacy
        ``ADMIN_PASSWORD`` configured, ``name`` may be empty or ``"admin"``.

        Failed logins are rate limited: 429 with ``Retry-After`` after
        ``ADMIN_LOGIN_MAX_ATTEMPTS`` failures for that ``(name, client IP)``.
        """
        return self._post("/auth/session", json={"name": name, "password": password})

    def logout(self) -> Dict[str, Any]:
        """``DELETE /auth/session`` — revoke the session token in use.

        Returns ``{"logged_out": bool}``; 400 when the client authenticates with
        HTTP Basic instead of a session token (there is nothing to revoke).
        """
        return self._request("DELETE", "/auth/session")

    def whoami(self) -> Dict[str, Any]:
        """``GET /auth/me`` — caller identity, role and session expiry.

        ``{"authenticated", "name", "role", "method", "roles",
        "session_expires_at", "session_idle_timeout_seconds"}``; ``method`` is
        ``session``, ``password``, ``legacy`` or ``anonymous`` (read-only mode).
        """
        return self._get("/auth/me")

    def list_sessions(self) -> Dict[str, Any]:
        """``GET /auth/sessions`` — live admin sessions (admin role).

        Never contains a token; each entry carries a short ``session_id`` for
        :meth:`revoke_session`.
        """
        return self._get("/auth/sessions")

    def revoke_session(self, session_id: str) -> Dict[str, Any]:
        """``DELETE /auth/sessions/{id}`` — revoke one session (admin role)."""
        return self._request("DELETE", f"/auth/sessions/{session_id}")

    def list_principals(self) -> Dict[str, Any]:
        """``GET /auth/principals`` — admin identities and roles (admin role)."""
        return self._get("/auth/principals")

    def create_principal(
        self,
        name: str,
        role: str,
        password: str,
        expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /auth/principals`` — create an identity with a role (admin role).

        Roles, lowest to highest: ``viewer`` (reads), ``operator`` (propose, apply
        approved, alerts/compliance writes), ``approver`` (approve / reject /
        rollback), ``admin`` (identity + fleet-wide). A role satisfies every role
        below it. The password is scrypt-hashed server-side and never returned.
        409 if the name is taken, 422 for a bad role or a password under 8 chars.
        """
        return self._post(
            "/auth/principals",
            json={
                "name": name,
                "role": role,
                "password": password,
                "expires_at": expires_at,
            },
        )

    def revoke_principal(self, principal_id: str) -> Dict[str, Any]:
        """``DELETE /auth/principals/{id}`` — revoke an identity (admin role).

        Closes every session it holds and keeps the row (the name stays reserved, so
        a revoked identity cannot be silently re-created by someone else).
        """
        return self._request("DELETE", f"/auth/principals/{principal_id}")

    # -------------------------------------------------------------------- audit

    def audit_log(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        target: Optional[str] = None,
        result: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """``GET /audit`` — privileged-action history, newest first (admin role).

        Returns ``{"entries", "total", "count", "limit", "offset", "filters",
        "enabled"}``. Each entry carries ``id``, ``ts``, ``actor``, ``actor_kind``
        (``human`` | ``agent`` | ``system``), ``role``, ``action``, ``target_kind``,
        ``target_id``, ``revision_id``, ``config_hash``, ``result`` (``ok`` |
        ``denied`` | ``error`` | ``noop``), redacted ``detail_json`` and the chain
        fields ``prev_hash``/``entry_hash``.

        ``action`` is a **prefix** match, so ``action="config."`` returns every
        config-push event. Never contains config bodies or secrets — hashes only.
        """
        return self._get(
            "/audit",
            params=_drop_empty(
                {
                    "since": since,
                    "until": until,
                    "actor": actor,
                    "action": action,
                    "target": target,
                    "result": result,
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )

    def audit_verify(self) -> Dict[str, Any]:
        """``GET /audit/verify`` — recompute the hash chain (admin role).

        ``{"ok", "entries", "checked", "broken_at", "reason",
        "anchored_by_checkpoint", ...}``. ``ok: false`` means a row was edited or
        removed: the log is not evidence any more. ``anchored_by_checkpoint: true``
        is benign — retention pruned older entries and recorded what it cut.
        """
        return self._get("/audit/verify")

    def audit_export(
        self,
        format: str = "jsonl",
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100000,
    ) -> str:
        """``GET /audit/export`` — the log as text, oldest first (admin role).

        ``format="jsonl"`` (one JSON object per line, what a SIEM ingests) or
        ``"csv"`` (fixed columns, for a spreadsheet). Returns the raw body, not
        parsed JSON — pipe it straight to a file.
        """
        return self._request_text(
            "GET",
            "/audit/export",
            params=_drop_empty({"format": format, "since": since, "until": until, "limit": limit}),
        )

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

    # ------------------------------------------------------------ remote config

    def get_agent_config(self, agent_id: str, reveal: bool = False) -> Dict[str, Any]:
        """``GET /agent/{id}/config`` — desired + effective config, **redacted**.

        Secret-looking keys and auth-shaped header values are replaced with
        ``<redacted>`` and listed in ``redacted_paths``. ``reveal=True`` returns the
        real values; it needs the ``approver`` role (or higher), is audited as
        ``config.reveal``, and is the only way to see a secret.

        ``desired`` is null until something has been pushed; ``failed_at`` means the
        agent reported FAILED and that revision is poisoned (never retried).
        """
        params = {"reveal": "true"} if reveal else None
        return self._get(f"/agent/{agent_id}/config", params=params)

    def set_agent_config(
        self,
        agent_id: str,
        config_map: Dict[str, Any],
        note: Optional[str] = None,
        allow_endpoint_change: bool = False,
    ) -> Dict[str, Any]:
        """``PUT /agent/{id}/config`` — push a desired config (approver).

        ``config_map`` maps a config object name to
        ``{"body", "content_type", "role"?}``; ``{"": {...}}`` is the single-object
        case, and ``role`` is the optional v0.20.0 ``AgentConfigObject.role`` the
        agent defines (carried verbatim). Returns the new ``revision_id``
        and ``config_hash``, plus a ``delivery`` object: ``delivered`` when the frame
        went to the agent's live WebSocket connection, ``queued`` when it will ride
        the next status report, ``unchanged`` when there was nothing to send. Never
        retried automatically.

        This is approval-by-fiat: the caller holds the approver role. For the
        two-phase flow use :meth:`propose_config` + :meth:`approve_config`.
        ``allow_endpoint_change=True`` is required if the body moves the `opamp`
        extension's endpoint away from this server (see #100).

        404 means config push is disabled (not forbidden): the surface does not
        exist until the posture is secure. 422 lists every validation error with a
        YAML path.
        """
        body: Dict[str, Any] = {"config_map": config_map}
        if note:
            body["note"] = note
        if allow_endpoint_change:
            body["allow_endpoint_change"] = True
        return self._put(f"/agent/{agent_id}/config", json=body)

    def delete_agent_config(self, agent_id: str) -> Dict[str, Any]:
        """``DELETE /agent/{id}/config`` — withdraw the desired config (approver).

        Withdrawal is an *empty* config map, which tells the agent to run its local
        config; it is delivered once, like any other revision.
        """
        return self._request("DELETE", f"/agent/{agent_id}/config")

    def apply_agent_config(
        self,
        config_map: Dict[str, Any],
        agents: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,
        confirm: bool = False,
        note: Optional[str] = None,
        group: Optional[str] = None,
        use_stored_resolution: bool = False,
        confirm_scope_change: bool = False,
    ) -> Dict[str, Any]:
        """``POST /config/apply`` — push one config to many agents (admin).

        Blast radius is explicit: pass ``agents=[...]``, ``filter={...}`` (the
        ``GET /agents`` filter keys) or ``group="prod-emea"`` (a saved selector,
        #111). A missing selector is a 400 — never "all". ``confirm=True`` is
        required, which is the deliberate speed bump before a fleet-wide change.

        A group target obeys the same guards as a proposal: a health-based definition
        needs ``use_stored_resolution=True``, a group that resolved smaller than last
        time needs ``confirm_scope_change=True``, and an empty group is a 404.
        """
        body: Dict[str, Any] = {"config_map": config_map, "confirm": confirm}
        if agents is not None:
            body["agents"] = agents
        if filter is not None:
            body["filter"] = filter
        if group is not None:
            body["group"] = group
        if use_stored_resolution:
            body["use_stored_resolution"] = True
        if confirm_scope_change:
            body["confirm_scope_change"] = True
        if note:
            body["note"] = note
        return self._post("/config/apply", json=body)

    def validate_config(
        self,
        config_map: Dict[str, Any],
        agent_id: Optional[str] = None,
        allow_endpoint_change: bool = False,
    ) -> Dict[str, Any]:
        """``POST /config/validate`` — check a config map without storing it.

        Returns ``{"valid", "errors", "details", "warnings", "policy",
        "fingerprint", "config_hash", "size_bytes", "objects"}``. ``errors`` are
        strings; ``details`` carry a YAML path, message and hint so a UI/CLI can
        point at the problem. Always 200: an invalid config is an answer, not an
        error. This is the call an AI agent should make before proposing anything
        (MCP may validate and propose, never approve or apply).

        ``agent_id`` adds that agent's component inventory to the checks, which is
        what catches "this config needs a component the agent does not ship".
        """
        body: Dict[str, Any] = {"config_map": config_map}
        if agent_id:
            body["agent_id"] = agent_id
        if allow_endpoint_change:
            body["allow_endpoint_change"] = True
        return self._post("/config/validate", json=body)

    # ------------------------------------------------- config control plane (#99)

    def propose_config(
        self,
        config_map: Dict[str, Any],
        scope: Dict[str, Any],
        note: Optional[str] = None,
        allow_endpoint_change: bool = False,
        override_reason: Optional[str] = None,
        confirm_scope_change: bool = False,
    ) -> Dict[str, Any]:
        """``POST /config/revisions`` — draft a revision (operator). **Delivers nothing.**

        ``scope`` is exactly one of ``{"agent_id": id}``, ``{"agents": [ids]}``,
        ``{"filter": {...}}`` (the ``GET /agents`` filter keys) or
        ``{"group": "prod-emea"}`` (#111), plus an optional ``include_future: true``
        on a selector or group scope. A group expands server-side into its definition
        plus pins and is snapshotted onto every revision. A health-based group needs
        ``use_stored_resolution: true``; a group that resolved smaller than its last
        resolution needs ``confirm_scope_change: true``. A missing scope is a 400: it
        is never "the whole fleet".

        Returns the ``proposal_id``, the per-agent revisions and the structured
        validation verdict. An approver must call :meth:`approve_config` before any
        agent sees it.
        """
        body: Dict[str, Any] = {"config_map": config_map, "scope": scope}
        if note:
            body["note"] = note
        if allow_endpoint_change:
            body["allow_endpoint_change"] = True
        if override_reason:
            body["override_reason"] = override_reason
        if confirm_scope_change:
            body["confirm_scope_change"] = True
        return self._post("/config/revisions", json=body)

    def list_config_revisions(
        self,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """``GET /config/revisions`` — revision metadata, newest first (viewer).

        **No config bodies** — hashes, authors, timestamps, lifecycle status, the
        stored verdict and delivery facts only. ``status`` filters on the lifecycle
        (``proposed``, ``active``, ``superseded``, ``rolled_back``, ``poisoned``,
        ``rejected``).
        """
        params: Dict[str, Any] = {"limit": limit}
        if agent:
            params["agent"] = agent
        if status:
            params["status"] = status
        return self._get("/config/revisions", params=params)

    def get_config_proposal(self, proposal_id: str) -> Dict[str, Any]:
        """``GET /config/revisions/{proposal_id}`` — one proposal with its revisions.

        Per-agent lifecycle status, the stored verdict and the delivery record
        (``delivered_hash``, ``attempts``, ``last_status``, ``poisoned``). Bodies are
        excluded; read one agent's desired config through :meth:`get_agent_config`.
        """
        return self._get(f"/config/revisions/{proposal_id}")

    def approve_config(
        self,
        proposal_id: str,
        note: Optional[str] = None,
        confirm_endpoint_change: bool = False,
        confirm_scope_change: bool = False,
        override_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /config/revisions/{id}/approve`` — activate a proposal (approver).

        Re-resolves the selector and re-runs the validation pipeline; a blocking
        change is a 409 (``re-validate: ...``), and a policy violation needs
        ``override_reason``, which is recorded and audited as ``config.override``.
        Supersedes each agent's previous revision (kept for rollback).

        If the selector now matches agents it did not at propose time, the call is
        refused until ``confirm_scope_change=True``: an approval that quietly widens
        its own blast radius is what the two-phase flow exists to prevent.
        """
        body: Dict[str, Any] = {}
        if note:
            body["note"] = note
        if confirm_endpoint_change:
            body["confirm_endpoint_change"] = True
        if confirm_scope_change:
            body["confirm_scope_change"] = True
        if override_reason:
            body["override_reason"] = override_reason
        return self._post(f"/config/revisions/{proposal_id}/approve", json=body)

    def reject_config(self, proposal_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """``POST /config/revisions/{id}/reject`` — refuse a proposal (approver).

        The revision is kept, marked ``rejected``: a proposed-and-refused config is
        evidence. Only ``proposed`` revisions can be rejected; an active one is
        withdrawn or rolled back instead (409).
        """
        body: Dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        return self._post(f"/config/revisions/{proposal_id}/reject", json=body)

    def reactivate_config(
        self,
        proposal_id: str,
        reason: Optional[str] = None,
        confirm_endpoint_change: bool = False,
        override_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /config/revisions/{id}/reactivate`` — resume a poisoned revision.

        A `FAILED` report or a detected auto-revert poisons a revision permanently;
        nothing retries on a timer. This is an approver's explicit decision, and it
        re-runs validation first (409 when the config still does not pass).
        """
        body: Dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        if confirm_endpoint_change:
            body["confirm_endpoint_change"] = True
        if override_reason:
            body["override_reason"] = override_reason
        return self._post(f"/config/revisions/{proposal_id}/reactivate", json=body)

    def deliver_agent_config(self, agent_id: str) -> Dict[str, Any]:
        """``POST /agent/{id}/config/deliver`` — deliver the active config now (operator).

        Re-delivery of an **already approved** revision: it moves no pointer and
        changes nothing, so it needs ``operator`` rather than ``approver``. Present
        because a push is an optimisation over the poll queue (#102), and "the queue
        dropped that frame" needs a lever that is not "wait for the next report".

        Returns ``{"agent_id", "delivery": {state, revision_id, config_hash,
        channel, reason, detail}}`` where ``state`` is:

        * ``delivered`` — handed to the agent's live WebSocket connection;
        * ``queued`` — no live connection (or throttled / refused); it arrives on the
          agent's next status report;
        * ``unchanged`` — nothing to deliver; ``reason`` says why
          (``reported_hash_matches``, ``revision_not_deliverable``, …).

        Always 200 for a known agent: ``queued`` is the designed fallback, not an
        error, so a rollout engine can call this in a loop.
        """
        return self._post(f"/agent/{agent_id}/config/deliver")

    # ------------------------------------------------ staged rollouts (#101/#103)

    def plan_config_rollout(
        self,
        proposal_id: str,
        canary_percent: Optional[float] = None,
        max_agents_per_batch: Optional[int] = None,
        max_percent_per_batch: Optional[float] = None,
        health_gate_seconds: Optional[int] = None,
        ttl_minutes: Optional[int] = None,
        restart_after_apply: bool = False,
    ) -> Dict[str, Any]:
        """``POST /config/rollouts?proposal_id=…`` with ``dry_run`` — plan, change nothing.

        Returns the exact batches (agent ids included), the canary size and the caps in
        force, plus the stored validation verdict. Nothing is approved, held or
        delivered, so this is safe to call in a loop and from an AI agent's "what would
        happen?" step.
        """
        return self.create_config_rollout(
            proposal_id,
            dry_run=True,
            canary_percent=canary_percent,
            max_agents_per_batch=max_agents_per_batch,
            max_percent_per_batch=max_percent_per_batch,
            health_gate_seconds=health_gate_seconds,
            ttl_minutes=ttl_minutes,
            restart_after_apply=restart_after_apply,
        )

    def create_config_rollout(
        self,
        proposal_id: str,
        dry_run: bool = False,
        note: Optional[str] = None,
        canary_percent: Optional[float] = None,
        max_agents_per_batch: Optional[int] = None,
        max_percent_per_batch: Optional[float] = None,
        health_gate_seconds: Optional[int] = None,
        ttl_minutes: Optional[int] = None,
        restart_after_apply: bool = False,
        confirm_endpoint_change: bool = False,
        confirm_scope_change: bool = False,
        override_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /config/rollouts`` — approve **and** stage a proposal (approver).

        Creating a rollout *is* the approval: the proposal is approved, every revision
        is assigned but held, and only the canary batch is released. The rest of the
        fleet is promoted one batch per gate pass, so nothing arrives without evidence
        that the canary came back healthy (#101).

        ``restart_after_apply=True`` queues a restart command (#84) for each agent once
        its batch passes the gate, which is how "this config needs a restart" stays one
        action. ``dry_run=True`` is :meth:`plan_config_rollout`.
        """
        body: Dict[str, Any] = {"dry_run": bool(dry_run)}
        if note:
            body["note"] = note
        for name, value in (
            ("canary_percent", canary_percent),
            ("max_agents_per_batch", max_agents_per_batch),
            ("max_percent_per_batch", max_percent_per_batch),
            ("health_gate_seconds", health_gate_seconds),
            ("ttl_minutes", ttl_minutes),
        ):
            if value is not None:
                body[name] = value
        if restart_after_apply:
            body["restart_after_apply"] = True
        if confirm_endpoint_change:
            body["confirm_endpoint_change"] = True
        if confirm_scope_change:
            body["confirm_scope_change"] = True
        if override_reason:
            body["override_reason"] = override_reason
        return self._post("/config/rollouts", params={"proposal_id": proposal_id}, json=body)

    def revalidate_config(self, proposal_id: str) -> Dict[str, Any]:
        """``POST /config/revisions/{id}/validate`` — re-run the pipeline, change nothing.

        Read-only: nothing is stored, no pointer moves and no poison is cleared. The
        pipeline runs against each resolved agent, so the inventory warnings are about
        the machines an approval would actually reach.
        """
        return self._post(f"/config/revisions/{proposal_id}/validate")

    def list_config_rollouts(
        self, status: Optional[str] = None, limit: int = 100
    ) -> Dict[str, Any]:
        """``GET /config/rollouts`` — rollouts newest first, with per-state counts.

        ``status`` is ``pending|running|paused|completed|aborted|cancelled``. Metadata
        only; never a config body.
        """
        params: Dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return self._get("/config/rollouts", params=params)

    def get_config_rollout(self, rollout_id: str) -> Dict[str, Any]:
        """``GET /config/rollouts/{id}`` — batch progress, per-agent states, pause reason.

        The per-agent ``state`` is ``pending|delivered|applying|applied|failed|
        reverted|paused``. A paused rollout carries exactly one ``paused_reason``
        (``failed``, ``auto_reverted``, ``agent_went_silent``, ``unhealthy``,
        ``delivery_unconfirmed``, ``superseded``) — and nothing is ever retried
        automatically.
        """
        return self._get(f"/config/rollouts/{rollout_id}")

    def abort_config_rollout(
        self, rollout_id: str, reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """``POST /config/rollouts/{id}/abort`` — stop promoting (operator).

        Does **not** recall configs that were already delivered, because it cannot. It
        stops the next batch immediately and leaves un-promoted agents held, so an
        aborted rollout can never widen. Takes effect within one engine tick.
        """
        body: Dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        return self._post(f"/config/rollouts/{rollout_id}/abort", json=body)

    def rollback_config_rollout(
        self,
        rollout_id: str,
        note: Optional[str] = None,
        health_gate_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """``POST /config/rollouts/{id}/rollback`` — undo this rollout, staged (approver).

        Creates a new revision per agent from the previous one (naming its parent) and
        runs it as **its own** rollout, canary first and gated. Rolling three hundred
        agents back at once is a second outage, so this is never automatic.
        """
        body: Dict[str, Any] = {}
        if note:
            body["note"] = note
        if health_gate_seconds is not None:
            body["health_gate_seconds"] = health_gate_seconds
        return self._post(f"/config/rollouts/{rollout_id}/rollback", json=body)

    def restart_agent(self, agent_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """``POST /agent/{id}/restart`` — queue a restart for the agent's next poll.

        Delivered **exactly once**, and a reply carrying a command carries nothing else
        (spec rule). Refused with **409** when the agent does not advertise
        ``AcceptsRestartCommand`` — upstream defaults that to ``false``, so the
        response's ``detail.hint`` names the flag to enable.
        """
        body: Dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        return self._post(f"/agent/{agent_id}/restart", json=body)

    def send_custom_message(
        self,
        agent_id: str,
        capability: str,
        type: str,
        data: str = "",
    ) -> Dict[str, Any]:
        """``POST /agent/{id}/custom-message`` — the spec's extension mechanism (#85).

        ``capability`` is a reverse FQDN the agent advertised in
        ``custom_capabilities`` (see :meth:`get_agent`), ``type`` is scoped to it and
        ``data`` is **base64** opaque bytes: the payload contract belongs to the
        capability, and the server neither interprets nor rewrites it.

        Refused with **400** when the agent does not advertise the capability — the
        detail lists ``supported_capabilities``. Delivered **exactly once**, on the
        agent's next message; the config offer waits one poll so a reply carries one
        server-initiated action.
        """
        body: Dict[str, Any] = {
            "capability": capability,
            "type": type,
            "data": data or "",
        }
        return self._post(f"/agent/{agent_id}/custom-message", json=body)

    def rollback_agent_config(
        self, agent_id: str, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """``POST /agent/{id}/config/rollback`` — return to the previous revision (approver).

        Rollback is a **new** revision whose body is the previous one and whose
        ``parent_revision_id`` names it (revisions are immutable, so a pointer move
        would make the audit trail lie). 409 when there is nothing to roll back to.
        """
        body: Dict[str, Any] = {}
        if note:
            body["note"] = note
        return self._post(f"/agent/{agent_id}/config/rollback", json=body)

    # ------------------------------------------------- collector groups (#111)

    def groups(self, include_archived: bool = False) -> Dict[str, Any]:
        """``GET /groups`` — saved selectors with **live** member counts (viewer).

        A group is a name for a filter document plus ``include``/``exclude`` pins;
        membership is resolved on read, never stored. ``last_resolution`` is what the
        group meant when it was last evaluated, which is the baseline
        :meth:`preview_group` compares against.
        """
        params = {"include_archived": True} if include_archived else None
        return self._get("/groups", params=params)

    def group(self, group_id: str) -> Dict[str, Any]:
        """``GET /groups/{id}`` — definition, current members, resolution history (viewer).

        ``sources`` separates members that came from the definition from pins,
        ``unknown_pins`` names pins for agents that have never reported, and
        ``membership_changes`` counts how often the resolved set moved.
        """
        return self._get(f"/groups/{group_id}")

    def create_group(
        self,
        name: str,
        definition: Dict[str, Any],
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        description: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /groups`` — create a named selector (operator).

        Names are unique regardless of case, may not collide with the reserved filter
        keys (``healthy``, ``status``, ``remote_config_status``), and a definition may
        not be empty (``{}`` would mean the whole fleet). A newly created group with
        zero members is valid; pushing to it is not.
        """
        body: Dict[str, Any] = {"name": name, "definition": definition}
        if include:
            body["include"] = include
        if exclude:
            body["exclude"] = exclude
        if description:
            body["description"] = description
        if owner:
            body["owner"] = owner
        return self._post("/groups", json=body)

    def update_group(
        self,
        group_id: str,
        name: Optional[str] = None,
        definition: Optional[Dict[str, Any]] = None,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        description: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``PUT /groups/{id}`` — edit a group (operator). Returns the definition diff.

        Editing changes **future** pushes only: a revision approved against the old
        definition keeps its ``group_snapshot``. ``changes`` carries the
        ``added``/``removed``/``changed`` diff that the audit row recorded.
        """
        body: Dict[str, Any] = {}
        for key, value in (
            ("name", name),
            ("definition", definition),
            ("include", include),
            ("exclude", exclude),
            ("description", description),
            ("owner", owner),
        ):
            if value is not None:
                body[key] = value
        return self._put(f"/groups/{group_id}", json=body)

    def archive_group(self, group_id: str) -> Dict[str, Any]:
        """``DELETE /groups/{id}`` — **archive** a group (operator). There is no delete.

        Revisions and rollouts reference the id, so history stays readable. A live
        rollout approved against the group refuses with 409 first. Archived groups
        cannot be pushed to or edited; :meth:`restore_group` brings one back.
        """
        return self._request("DELETE", f"/groups/{group_id}")

    def restore_group(self, group_id: str) -> Dict[str, Any]:
        """``POST /groups/{id}/restore`` — un-archive a group (operator)."""
        return self._post(f"/groups/{group_id}/restore", json={})

    def preview_group(self, group_id: str) -> Dict[str, Any]:
        """``POST /groups/{id}/preview`` — members now vs last resolution (viewer).

        ``added`` / ``dropped`` / ``unchanged`` against the last recorded resolution,
        with ``sources`` naming which members came from the definition and which from
        pins. No side effects beyond recording the resolution as the next baseline.
        This is also how a **health-defined** group gets a membership for
        ``use_stored_resolution``.
        """
        return self._post(f"/groups/{group_id}/preview", json={})

    def attribute_keys(self) -> Dict[str, Any]:
        """``GET /groups/attribute-keys`` — keys and sample values from live agents (viewer).

        Built for typo-proof authoring: ``environment`` and its observed values come
        from the fleet, so a definition cannot be written against ``env`` or
        ``prod-*`` (matching is exact, case-insensitive equality). Reads agent
        descriptions only, never an effective config.
        """
        return self._get("/groups/attribute-keys")

    # ------------------------------------------------------------- credentials

    def create_agent_token(
        self,
        agent_id: str,
        label: str = "",
        retire_previous: bool = False,
    ) -> Dict[str, Any]:
        """``POST /agents/{id}/token`` — mint a per-agent credential (admin).

        For ``OPAMP_AGENT_AUTH=token``. ``agent_id`` is the hex ``instance_uid`` the
        credential is bound to, so the credential may only ever speak for that one
        agent. Returns ``{"agent_id", "token", "label", "created_at",
        "collector_snippet", "note"}`` — the token is shown **once** and only its
        SHA-256 is stored, so keep it. ``collector_snippet`` is a paste-ready
        ``extensions.opamp.server.<http|ws>.headers`` block.

        ``retire_previous=True`` revokes the agent's older credentials immediately;
        the default retires them on the first successful use of the new token, which
        is what makes rotation zero-downtime.
        """
        body = {"label": label, "retire_previous": retire_previous}
        return self._post(f"/agents/{agent_id}/token", json=body)

    def revoke_agent_token(self, agent_id: str) -> Dict[str, Any]:
        """``DELETE /agents/{id}/token`` — revoke every live credential (admin).

        Returns ``{"agent_id", "revoked": n}``; idempotent (0 when none were live).
        Revocation blocks delivery and keeps the agent's state and desired config.
        """
        return self._request("DELETE", f"/agents/{agent_id}/token")

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
