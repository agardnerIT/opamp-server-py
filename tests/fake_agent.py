"""A fake OpAMP agent that behaves like the upstream OpAMP Supervisor (#107).

The security regression suite needs an agent that is *scriptable*, not a real
collector: one that can accept a config, reject it, silently revert to its last
working config (the T9 trap), go quiet, or crash. `tests/test_ws.py` already has a
`TestClient` WebSocket harness and `tests/asgi_transport.py` an in-process HTTP
transport; this module is the missing third piece — the *agent* — and it uses both.

Design notes:

* **Both transports, one state machine.** `poll_http()` and `poll_ws()` send the
  same `AgentToServer` and feed the reply through the same `receive()`, so a
  transport-parity test cannot pass for the wrong reason.
* **Modes.** `apply_ok` (APPLYING → APPLIED on a scripted schedule), `apply_fail`
  (reports `FAILED` with an error message), `auto_revert` (mimics the supervisor:
  falls back to the *last working* config and reports `APPLIED` for **that** hash —
  the T9 trap), `silent` (never reports; goes stale), `crash` (accepts the config
  and never comes back) and `refuse` (does not advertise `AcceptsRemoteConfig`, so
  the server must never offer it a config at all).
* **No sleeps.** Time-based server behaviour is controlled the way the product
  already allows it: `CONFIG_REVERT_GRACE_SECONDS=0`, `AGENT_TIMEOUT_*` and direct
  manipulation of `agent.last_heartbeat`. A test that needs "an hour later" calls
  `fake_agent.age_the_server(3600)`; nothing waits.
* **Hashes are bytes on the wire.** The fake agent mirrors what the supervisor does:
  it stores the opaque hash it received and echoes it back; it never re-derives it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from proto import anyvalue_pb2, opamp_pb2
from server.framing import decode_message, encode_message
from server.state import AGENT_REGISTRY

STATUS_NAMES = {
    "UNSET": opamp_pb2.RemoteConfigStatuses.RemoteConfigStatuses_UNSET,
    "APPLIED": opamp_pb2.RemoteConfigStatuses.RemoteConfigStatuses_APPLIED,
    "APPLYING": opamp_pb2.RemoteConfigStatuses.RemoteConfigStatuses_APPLYING,
    "FAILED": opamp_pb2.RemoteConfigStatuses.RemoteConfigStatuses_FAILED,
}

MODES = ("apply_ok", "apply_fail", "auto_revert", "silent", "crash", "refuse")

DEFAULT_CAPABILITIES = (
    opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    | opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
    | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsEffectiveConfig
)


def _config_map(reply: opamp_pb2.AgentRemoteConfig) -> Dict[str, str]:
    return {
        key: entry.body.decode("utf-8", "replace")
        for key, entry in reply.config.config_map.items()
    }


@dataclass
class FakeAgent:
    """One scriptable agent identity.

    `script` is consumed one entry per accepted config: `["APPLYING", "APPLIED"]`
    reports APPLYING on the poll after delivery and APPLIED on the one after that.
    When the script is exhausted the mode's default applies.
    """

    uid: bytes
    mode: str = "apply_ok"
    capabilities: int = DEFAULT_CAPABILITIES
    healthy: bool = True
    script: List[str] = field(default_factory=list)
    error_message: str = "collector refused the config"
    report_effective_config: bool = True
    description: Optional[Dict[str, str]] = None
    # Filled in by the test with the agent's bearer header; the fake agent does not
    # mint its own credentials (that is the server's job, #94).
    headers: Dict[str, str] = field(default_factory=dict)

    # -- agent-side state ----------------------------------------------------
    last_working: Dict[str, str] = field(default_factory=dict)
    last_working_hash: Optional[bytes] = None
    reported_config: Dict[str, str] = field(default_factory=dict)
    reported_hash: Optional[bytes] = None
    status: Optional[str] = None
    last_error: str = ""
    deliveries: List[Dict[str, str]] = field(default_factory=list)
    stopped: bool = False
    disconnected: bool = False
    transport: Optional[str] = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown mode {self.mode!r}; expected one of {MODES}")
        if self.mode == "refuse":
            self.capabilities &= ~opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig

    # ------------------------------------------------------------------ wire

    @property
    def agent_id(self) -> str:
        return self.uid.hex()

    def message(self, *, disconnect: bool = False) -> bytes:
        """The next `AgentToServer`: current status, hash and (optionally) config."""
        msg = opamp_pb2.AgentToServer()
        msg.instance_uid = self.uid
        msg.capabilities = self.capabilities
        msg.health.healthy = self.healthy

        if self.description:
            for key, value in self.description.items():
                msg.agent_description.identifying_attributes.add(
                    key=key, value=anyvalue_pb2.AnyValue(string_value=value)
                )

        if self.status:
            msg.remote_config_status.status = STATUS_NAMES[self.status]
            msg.remote_config_status.error_message = self.last_error
            if self.reported_hash:
                msg.remote_config_status.last_remote_config_hash = self.reported_hash

        if self.report_effective_config and self.reported_config:
            for key, body in self.reported_config.items():
                msg.effective_config.config_map.config_map[key].body = body.encode()

        if disconnect:
            msg.agent_disconnect.SetInParent()

        # Advance a scripted schedule *after* building this message: APPLYING is
        # reported once, then the script's next entry (or APPLIED).
        if self.status == "APPLYING" and self.script:
            self.status = self.script.pop(0)
        return msg.SerializeToString()

    def receive(self, reply: opamp_pb2.ServerToAgent) -> None:
        """Apply a `ServerToAgent` exactly as the supervisor would (mode-dependent)."""
        if not reply.HasField("remote_config"):
            return

        config = _config_map(reply.remote_config)
        remote_hash = bytes(reply.remote_config.config_hash)
        self.deliveries.append(config)

        if self.mode == "refuse":
            # Must never happen: the server must not offer a config to an agent that
            # did not advertise AcceptsRemoteConfig. Recorded so the test can assert.
            return

        if self.mode == "silent":
            # Applied, but the agent never says so: the server sees silence.
            return

        if self.mode == "crash":
            # The supervisor died restarting the collector: no report, ever.
            self.stopped = True
            return

        if self.mode == "apply_fail":
            self.status = "FAILED"
            self.last_error = self.error_message
            self.reported_hash = remote_hash
            self.reported_config = config
            return

        if self.mode == "auto_revert":
            # The collector rejected the config and the supervisor restored
            # `last_working_remote_config.dat`: it reports APPLIED for the *old*
            # hash — the trap that makes a naive hash comparison loop forever.
            self.status = "APPLIED"
            self.reported_config = dict(self.last_working)
            self.reported_hash = self.last_working_hash
            self.last_error = ""
            return

        # apply_ok: the first report is a scripted APPLYING, then APPLIED.
        self.last_working = config
        self.last_working_hash = remote_hash
        self.reported_config = config
        self.reported_hash = remote_hash
        self.status = self.script.pop(0) if self.script else "APPLIED"
        self.last_error = ""

    # -------------------------------------------------------------- transports

    def poll_http(
        self,
        client: TestClient,
        *,
        headers: Optional[Dict[str, str]] = None,
        disconnect: bool = False,
    ) -> Optional[opamp_pb2.ServerToAgent]:
        """One HTTP round trip. Returns None when the agent has crashed."""
        if self.stopped:
            return None
        response = client.post(
            "/v1/opamp",
            content=self.message(disconnect=disconnect),
            headers={"Content-Type": "application/x-protobuf", **(headers or {})},
        )
        self.transport = "http"
        if response.status_code != 200:
            raise AssertionError(
                f"agent {self.agent_id} got HTTP {response.status_code}: {response.text}"
            )
        reply = opamp_pb2.ServerToAgent()
        reply.ParseFromString(response.content)
        self.receive(reply)
        return reply

    def poll_ws(
        self,
        websocket: Any,
        *,
        disconnect: bool = False,
    ) -> Optional[opamp_pb2.ServerToAgent]:
        """One WebSocket round trip on an already-open socket."""
        if self.stopped:
            return None
        websocket.send_bytes(encode_message(self.message(disconnect=disconnect)))
        reply = opamp_pb2.ServerToAgent()
        reply.ParseFromString(decode_message(websocket.receive_bytes()))
        self.transport = "ws"
        self.receive(reply)
        return reply

    # ------------------------------------------------------------------ helpers

    def drive_http(
        self,
        client: TestClient,
        polls: int,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> List[opamp_pb2.ServerToAgent]:
        """Poll `polls` times, feeding each reply back into the agent state."""
        replies = []
        for _ in range(polls):
            reply = self.poll_http(client, headers=headers)
            if reply is None:
                break
            replies.append(reply)
        return replies

    @staticmethod
    def count_offers(replies: List[opamp_pb2.ServerToAgent]) -> int:
        return sum(1 for reply in replies if reply.HasField("remote_config"))

    def age_the_server(self, seconds: float) -> None:
        """Time-warp: move this agent's last heartbeat into the past.

        Server staleness is computed from `last_heartbeat`, which is what the
        periodic sweep reads — no sleep, no clock monkeypatch, no flakiness.
        """
        agent = AGENT_REGISTRY.get(self.agent_id)
        if agent is not None:
            agent.last_heartbeat = agent.last_heartbeat - timedelta(seconds=seconds)

    def report(self, status: str, error: str = "") -> None:
        """Stage a status report for the next poll (test convenience)."""
        self.status = status
        self.last_error = error
        if status == "FAILED" and self.reported_hash is None:
            self.reported_hash = self.last_working_hash

    def disconnect(self, client: TestClient) -> Optional[opamp_pb2.ServerToAgent]:
        reply = self.poll_http(client, disconnect=True)
        self.disconnected = True
        return reply


__all__ = ["FakeAgent", "MODES", "DEFAULT_CAPABILITIES", "STATUS_NAMES"]
