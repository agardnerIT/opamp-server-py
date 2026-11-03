"""One way to speak OpAMP to this server from a test (#80).

`tests/conformance/` is the **spec index**: one case per clause of the OpAMP
specification, named after the requirement rather than after the issue that
implemented it, so a spec bump is a diff of cases instead of a fresh code review.
The deep behavioural suites (`tests/test_sequence.py`, `test_http_limits.py`, ...)
stay where they are; a conformance case is deliberately thin and names the suite
that owns the detail.

This module is the transport half. It wraps the in-process ASGI transport from
`tests/asgi_transport.py` — not `TestClient`, whose internals moved between
starlette releases and which is deprecated against the installed httpx — and adds
the message builders a conformance case needs.

Two deliberate differences from the sketch in the issue:

* `send()` returns the raw `httpx.Response`, not a parsed `ServerToAgent`, because
  half the requirements are about the *envelope* (413, 415, 400, gzip). `expect()`
  is the parsed-reply shorthand for the cases where 200 is the requirement.
* builders are named for the spec shapes (`hash_only_components`, `heartbeat`,
  `request_instance_uid`), not for a generic kwargs bag, so a case reads like the
  sentence it tests.

The spec is pinned, not vendored: `SPEC_VERSION`/`SPEC_COMMIT` are cited in every
case's docstring and in `docs/spec-conformance.md`, because this repository does not
carry `specification.md` and line numbers in an upstream document rot.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
from typing import Any, Dict, Mapping, Optional

import httpx

from proto import anyvalue_pb2, opamp_pb2
from tests.asgi_transport import InProcessASGITransport
from tests.uids import uid

#: The specification release this suite is written against, and its commit, so a case
#: can be checked against the exact text it was written for.
SPEC_VERSION = "v0.20.0"
SPEC_COMMIT = "b08624151d000c1ca043b300e1f5e3bc93107987"

OPAMP_PATH = "/v1/opamp"
PROTOBUF = "application/x-protobuf"

#: What a collector with the opamp extension actually advertises. Not every case uses
#: it, but a fixture that advertises nothing cannot receive a remote config and makes
#: capability tests accidentally trivial.
DEFAULT_CAPABILITIES = (
    opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsHealth
    | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsEffectiveConfig
    | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsAvailableComponents
)


def opamp_send(
    http: Any,
    body: Any = None,
    *,
    headers: Optional[Mapping[str, str]] = None,
    gzip_body: bool = False,
    raw_body: Optional[bytes] = None,
    content_length: Optional[int] = None,
) -> Any:
    """POST one OpAMP message and return the raw response.

    `http` is anything with `post(path, content=..., headers=...)` — `TestClient` or an
    `httpx.Client` over the in-process ASGI transport — so this is the single
    implementation of the request envelope for every suite (#112). `body` may be an
    `AgentToServer` or already-serialised bytes; `raw_body` replaces it when the point
    of the test is a body that is not a valid message.
    """
    if raw_body is not None:
        payload = raw_body
    elif body is None:
        payload = b""
    elif isinstance(body, (bytes, bytearray)):
        payload = bytes(body)
    elif hasattr(body, "SerializeToString"):
        payload = body.SerializeToString()
    else:
        # An iterable/stream goes through untouched: that is how a chunked body with no
        # `Content-Length` is sent, which is the only way to exercise the streaming cap.
        payload = body

    request_headers: Dict[str, str] = {"Content-Type": PROTOBUF}
    if gzip_body:
        assert isinstance(payload, (bytes, bytearray)), "gzip_body needs a byte body"
        payload = gzip.compress(bytes(payload))
        request_headers["Content-Encoding"] = "gzip"
    if content_length is not None:
        request_headers["Content-Length"] = str(content_length)
    request_headers.update(headers or {})
    return http.post(OPAMP_PATH, content=payload, headers=request_headers)


def handle_message(
    body: bytes,
    principal: Any = None,
    transport: str = "http",
    uid_header: str = "",
) -> opamp_pb2.ServerToAgent:
    """Drive the OpAMP message handler directly (no HTTP) and parse the reply.

    For suites whose subject is the *decision* rather than the envelope — config
    push, rollouts — where a socket would only add a status code to assert. The
    handler is the same one both transports call, so this cannot diverge from what an
    agent would receive.

    Imported lazily: this module is imported by suites that never touch the server
    module graph, and `server.main` binds its stores at import time (#112).
    """
    from server.main import _handle_agent_message

    processed = _handle_agent_message(body, principal, transport, uid_header)
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(processed.response)
    return reply


def parse_reply(response: Any) -> opamp_pb2.ServerToAgent:
    """Parse a `ServerToAgent` body without judging the status code."""
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(response.content)
    return reply


def expect_status(response: Any, status: int = 200) -> Any:
    """Assert the status code and hand the response back, for call chaining."""
    assert response.status_code == status, (
        f"expected {status}, got {response.status_code}: {response.text!r}"
    )
    return response


def expect_reply(response: Any, status: int = 200) -> opamp_pb2.ServerToAgent:
    """Assert the status code and parse the `ServerToAgent` reply."""
    return parse_reply(expect_status(response, status))


class OpampHarness:
    """An in-process OpAMP client. One per test, like the server it drives."""

    def __init__(self, app: Any, *, base_url: str = "http://opamp.test"):
        self._http = httpx.Client(
            transport=InProcessASGITransport(app), base_url=base_url
        )

    # -- HTTP -------------------------------------------------------------

    def send(self, message: Any = None, **send_kwargs: Any) -> Any:
        """`opamp_send()` over this harness's client (see that function for options)."""
        return opamp_send(self._http, message, **send_kwargs)

    def expect(
        self,
        message: Any = None,
        *,
        status: int = 200,
        **send_kwargs: Any,
    ) -> opamp_pb2.ServerToAgent:
        """`send()` and parse the reply, asserting the status code first."""
        return expect_reply(self.send(message, **send_kwargs), status)

    def get(self, path: str) -> httpx.Response:
        """Read an admin/agent endpoint from the same in-process server."""
        return self._http.get(path)

    def agent(self, agent_id: str) -> Dict[str, Any]:
        """`GET /agent/{id}` as a dict — the fleet view a conformance case asserts on."""
        response = self.get(f"/agent/{agent_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def close(self) -> None:
        self._http.close()


# -- message builders ---------------------------------------------------------


def agent_message(
    uid: bytes,
    *,
    sequence_num: int = 0,
    capabilities: int = DEFAULT_CAPABILITIES,
    flags: int = 0,
) -> opamp_pb2.AgentToServer:
    """The skeleton every builder starts from: uid, counter, capabilities, flags."""
    message = opamp_pb2.AgentToServer()
    message.instance_uid = uid
    message.sequence_num = sequence_num
    message.capabilities = capabilities
    message.flags = flags
    return message


def status_only(
    uid: bytes, *, sequence_num: int = 0, healthy: bool = True, last_error: str = ""
):
    """§Agent Status Compression: a message carrying only what changed.

    `last_error` exists because of a proto3 trap: `healthy = False` is the *default*
    value, so an unhealthy health message with nothing else in it serialises to an
    absent `health` sub-message and the server never sees it. A real agent says why.
    """
    message = agent_message(uid, sequence_num=sequence_num)
    message.health.healthy = healthy
    if last_error:
        message.health.last_error = last_error
    return message


def heartbeat(uid: bytes, *, sequence_num: int = 1):
    """The smallest legal message: identity and counter, every sub-message omitted."""
    return agent_message(uid, sequence_num=sequence_num)


def first_report(uid: bytes, *, sequence_num: int = 1, healthy: bool = True):
    """§Initial Handshake: description, health, config, components and packages (#67)."""
    message = agent_message(uid, sequence_num=sequence_num)
    attribute = message.agent_description.identifying_attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = "conformance-collector"
    message.health.healthy = healthy
    message.effective_config.config_map.config_map[""].body = (
        b"receivers:\n  otlp:\nservice:\n  pipelines:\n    traces:\n      receivers: [otlp]\n"
    )
    message.available_components.hash = component_hash("first-report")
    message.available_components.components["receivers"].sub_component_map[
        "otlp"
    ].metadata.add(key="code.namespace", value=_string(".../receiver/otlpreceiver v0.98.0"))
    message.package_statuses.packages["collector"].name = "collector"
    message.package_statuses.packages["collector"].agent_has_version = "1.0.0"
    return message


def hash_only_components(uid: bytes, *, sequence_num: int = 1, seed: str = "v1"):
    """§AvailableComponents: the `hash` without `components` — legal, and useless alone."""
    message = agent_message(uid, sequence_num=sequence_num)
    message.available_components.hash = component_hash(seed)
    return message


def full_components(uid: bytes, *, sequence_num: int = 1, seed: str = "v1"):
    """§AvailableComponents: the full list, plus the health that marks a component used.

    `used` is what the OCB manifest needs, so the health map names the same receiver:
    a component list alone yields a manifest with nothing in it.
    """
    message = agent_message(uid, sequence_num=sequence_num)
    message.available_components.hash = component_hash(seed)
    message.available_components.components["receivers"].sub_component_map["otlp"].metadata.add(
        key="code.namespace", value=_string(".../receiver/otlpreceiver v0.98.0")
    )
    pipeline = message.health.component_health_map["pipeline:traces"]
    pipeline.component_health_map["receiver:otlp"].healthy = True
    message.health.healthy = True
    return message


def request_instance_uid(current_uid: Optional[bytes] = None):
    """§Requesting instance_uid: an agent asking to be given a (new) identity.

    The flag travels with whatever uid the agent has *now* — a supervisor that lost its
    stored identity still has to name itself well enough to be answered, and an empty
    uid cannot be filed at all (see the empty-uid case in the suite).
    """
    message = opamp_pb2.AgentToServer()
    message.flags = (
        opamp_pb2.AgentToServerFlags.AgentToServerFlags_RequestInstanceUid
    )
    message.instance_uid = current_uid if current_uid is not None else uid("requesting-uid")
    message.sequence_num = 1
    message.capabilities = DEFAULT_CAPABILITIES
    return message


def disconnect(uid: bytes, *, sequence_num: int = 2):
    """§Agent Disconnect: the agent is going away, and asks for no reply."""
    message = agent_message(uid, sequence_num=sequence_num)
    message.agent_disconnect.SetInParent()
    return message


def remote_config_status(
    uid: bytes, *, status: str = "APPLIED", config_hash: bytes = b"\x01" * 32, sequence_num: int = 2
):
    """§RemoteConfigStatus: the agent's word on the config it was offered."""
    message = agent_message(uid, sequence_num=sequence_num)
    # The enum symbols are prefixed in the generated module, so `status="APPLIED"` is
    # mapped rather than guessed at — a typo must fail loudly, not silently send UNSET.
    message.remote_config_status.status = getattr(
        opamp_pb2.RemoteConfigStatuses, f"RemoteConfigStatuses_{status}"
    )
    message.remote_config_status.last_remote_config_hash = config_hash
    return message


def unsupported_optional_message(uid: bytes, *, sequence_num: int = 2):
    """One message carrying every optional shape this server does not implement.

    #82 (packages) was closed as not planned and #83 (connection settings) is still
    open, so the requirement this pins is forward compatibility: a server MUST NOT
    break on a legal message it does not understand. #85 (custom capabilities/messages)
    is implemented, so this message also checks that the parts we *do* understand are
    still stored.
    """
    message = agent_message(uid, sequence_num=sequence_num)
    message.package_statuses.packages["collector"].name = "collector"
    message.package_statuses.packages["collector"].status = (
        opamp_pb2.PackageStatusEnum.PackageStatusEnum_Installing
    )
    message.connection_settings_request.opamp.certificate_request.csr = b"csr"
    message.custom_capabilities.capabilities.append("conformance.example")
    message.custom_message.capability = "conformance.example"
    message.custom_message.type = "ping"
    message.custom_message.data = b"{}"
    return message


# -- reply accessors ----------------------------------------------------------


def _as_reply(value: Any) -> opamp_pb2.ServerToAgent:
    """Accept either a parsed reply or a raw HTTP response.

    Test suites differ in whether they assert on the envelope or the message; taking
    both keeps call sites readable without a per-file wrapper (#112). A raw response is
    required to be a 200 first: flag accessors are used for *negative* assertions too,
    and "no flags" must never be satisfied by a message the server refused.
    """
    if isinstance(value, opamp_pb2.ServerToAgent):
        return value
    return expect_reply(value)


def has_flag(reply: Any, flag: int) -> bool:
    return bool(_as_reply(reply).flags & flag)


def asks_for_full_state(reply: Any) -> bool:
    return has_flag(
        reply, opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState
    )


def asks_for_components(reply: Any) -> bool:
    return has_flag(
        reply,
        opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportAvailableComponents,
    )


# -- values -------------------------------------------------------------------


def component_hash(seed: str) -> bytes:
    """A deterministic, opaque component hash. The server never recomputes it."""
    return hashlib.sha256(seed.encode()).digest()


def _string(value: str) -> anyvalue_pb2.AnyValue:
    return anyvalue_pb2.AnyValue(string_value=value)


def b64(value: bytes) -> str:
    """Base64, the form a `bytes` field takes in `MessageToDict` JSON."""
    return base64.b64encode(value).decode()
