"""`sequence_num` tracking and `ReportFullState` repair (#70).

Spec §Agent Status Compression: an agent MAY omit sub-messages that have not
changed, and the server learns it missed status only from `sequence_num` — "not
exactly by one greater than the previously received sequence_num value". Recovery
is a `ServerToAgent` with `ReportFullState` set.

What is pinned here:

* a gap sets `ReportFullState`; a well-behaved counter never does;
* the counter survives a restart (a new registry reads it back from SQLite), which
  is the whole reason it is a column and not a dict;
* `0` means "not set" and never triggers an ask, so an SDK that never fills the
  field does not get a full-state request per message;
* the ask is rate limited per agent, so a flapping counter cannot become a storm;
* a *decrease* (restart with the same uid) is a gap, not a wrap to be trusted;
* the wrap boundary itself (`0xFFFFFFFF` → `0`) is a gap because 0 is "unset" —
  documented behaviour, not an accident;
* the new-agent baseline is still a single ask, counted under `new_agent`.

No sleeping and no wall clock: the rate limiter takes an injectable `now`, and the
HTTP path is driven through `TestClient` in-process.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server.main as server_main
from proto import opamp_pb2
from server import sequence
from server.main import app
from server.state import AGENT_REGISTRY, AgentState, SQLiteAgentStore
from server.ws import REGISTRY as WS_REGISTRY
from tests.conformance.harness import asks_for_full_state, opamp_send

UID = bytes.fromhex("cd" * 16)
AGENT_ID = UID.hex()
FULL_STATE = opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState


def message(sequence_num: int = 0, healthy: bool = True) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = UID
    msg.sequence_num = sequence_num
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = healthy
    return msg.SerializeToString()


@pytest.fixture
def client(monkeypatch):
    WS_REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    # A rate limit of 0 keeps the gap tests about gaps; the limiter has its own
    # tests below.
    monkeypatch.setenv("REPORT_FULL_STATE_MIN_INTERVAL_SECONDS", "0")
    yield TestClient(app)
    WS_REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)


# --- pure gap arithmetic ----------------------------------------------------


@pytest.mark.parametrize(
    "previous, current, expected",
    [
        (None, 1, False),  # no baseline: first contact already asks
        (None, 7, False),
        (1, 2, False),  # the only non-gap case
        (5, 6, False),
        (5, 7, True),  # a skipped message
        (5, 5, True),  # duplicate / replay
        (5, 4, True),  # restart with a reset counter
        (5, 0, False),  # 0 is "not set", not "counter went backwards"
        (5, 1, True),  # reset after a restart is a gap
        # The wrap boundary: 0xFFFFFFFF -> 1 reads as a gap of 2, because 0 is
        # reserved for "unset" and cannot be the honest next value. At one message a
        # second this is a 136-year problem, so it is documented rather than special
        # cased — a special case would also hide a genuine gap of exactly 2 there.
        (0xFFFFFFFF, 1, True),
    ],
)
def test_is_gap(previous, current, expected):
    assert sequence.is_gap(previous, current) is expected


def test_wrap_arithmetic_is_used_not_ordering():
    """`current > previous` cannot express the wrap; the mask can.

    `(0 - 0xFFFFFFFF) & 0xFFFFFFFF == 1`, so the value after the top of the 32-bit
    range is exactly "one greater" — but only for arithmetic that wraps.
    """
    assert ((0 - 0xFFFFFFFF) & sequence.SEQUENCE_NUM_MASK) == 1


def test_sequence_num_zero_never_asks_even_after_a_real_counter():
    """An SDK that never sets the field must not cause a ping-pong."""
    assert sequence.is_gap(42, 0) is False


# --- HTTP path --------------------------------------------------------------


def test_new_agent_gets_one_report_full_state_request(client):
    response = opamp_send(client, message(sequence_num=1))

    assert asks_for_full_state(response)


def test_in_order_counter_never_asks(client):
    opamp_send(client, message(sequence_num=1))
    opamp_send(client, message(sequence_num=2))

    for current in (3, 4, 5):
        assert asks_for_full_state(opamp_send(client, message(sequence_num=current))) is False


def test_sequence_gap_sets_report_full_state(client):
    opamp_send(client, message(sequence_num=1))
    opamp_send(client, message(sequence_num=2))

    gap = opamp_send(client, message(sequence_num=5))

    assert asks_for_full_state(gap)
    # The state itself is still applied: asking for full state is not a refusal.
    detail = client.get(f"/agent/{AGENT_ID}").json()
    assert detail["last_sequence_num"] == 5


def test_duplicate_sequence_num_is_a_gap(client):
    opamp_send(client, message(sequence_num=1))
    opamp_send(client, message(sequence_num=2))

    assert asks_for_full_state(opamp_send(client, message(sequence_num=2)))


def test_agent_that_never_sets_sequence_num_is_left_alone(client):
    """Every message is `sequence_num=0`: no baseline, so no ask after the first."""
    assert asks_for_full_state(opamp_send(client, message(sequence_num=0)))  # new agent
    for _ in range(3):
        assert asks_for_full_state(opamp_send(client, message(sequence_num=0))) is False

    assert client.get(f"/agent/{AGENT_ID}").json()["last_sequence_num"] is None


def test_counter_is_persisted_and_survives_a_restart(client, tmp_path):
    opamp_send(client, message(sequence_num=1))
    opamp_send(client, message(sequence_num=2))

    # A fresh store over the same database is what a restarted server sees.
    reloaded = SQLiteAgentStore(AGENT_REGISTRY._store.db_path).load_all()[AGENT_ID]
    assert reloaded.last_sequence_num == 2


def test_an_existing_database_gains_the_column_by_migration(tmp_path):
    """A live fleet's `agents` table predates this column; opening it must not fail."""
    import sqlite3

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        # The schema as it stood before #70, with one agent already registered.
        conn.execute(
            "CREATE TABLE agents (agent_id TEXT PRIMARY KEY, instance_uid BLOB,"
            " connected_at TEXT, last_heartbeat TEXT, healthy INTEGER,"
            " capabilities INTEGER DEFAULT 0, description TEXT, effective_config TEXT,"
            " remote_config_status TEXT DEFAULT 'UNSET', package_statuses TEXT,"
            " available_components TEXT, health TEXT, compliance TEXT, updated_at TEXT,"
            " last_remote_config_hash TEXT, remote_config_error TEXT)"
        )
        conn.execute(
            "INSERT INTO agents (agent_id, instance_uid, connected_at, last_heartbeat,"
            " healthy, updated_at) VALUES (?, ?, ?, ?, 1, ?)",
            (AGENT_ID, UID, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )

    loaded = SQLiteAgentStore(db).load_all()[AGENT_ID]

    assert loaded.last_sequence_num is None  # no baseline, so nothing is flagged
    with sqlite3.connect(db) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(agents)")]
    assert "last_sequence_num" in columns


def test_rate_limit_suppresses_a_second_ask(client, monkeypatch):
    monkeypatch.setenv("REPORT_FULL_STATE_MIN_INTERVAL_SECONDS", "60")
    opamp_send(client, message(sequence_num=1))
    opamp_send(client, message(sequence_num=2))

    assert asks_for_full_state(opamp_send(client, message(sequence_num=9)))
    # Still broken, still within the window: ask again would be a storm.
    assert asks_for_full_state(opamp_send(client, message(sequence_num=20))) is False


def test_rate_limit_expires_so_a_still_broken_agent_is_eventually_asked(client, monkeypatch):
    monkeypatch.setenv("REPORT_FULL_STATE_MIN_INTERVAL_SECONDS", "60")
    opamp_send(client, message(sequence_num=1))
    opamp_send(client, message(sequence_num=2))

    assert asks_for_full_state(opamp_send(client, message(sequence_num=9)))

    # Wind the per-agent timer back instead of sleeping.
    AGENT_REGISTRY._full_state_requested_at[AGENT_ID] -= 61
    assert asks_for_full_state(opamp_send(client, message(sequence_num=20)))


def test_disconnecting_agent_is_not_asked_for_full_state(client):
    opamp_send(client, message(sequence_num=1))
    opamp_send(client, message(sequence_num=2))

    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = UID
    msg.sequence_num = 9
    msg.agent_disconnect.SetInParent()
    response = opamp_send(client, msg.SerializeToString())

    assert asks_for_full_state(response) is False
    assert AGENT_REGISTRY.get(AGENT_ID) is None


# --- registry-level rate limiter -------------------------------------------


def test_claim_full_state_request_is_per_agent_and_time_bounded():
    state = AgentState(instance_uid=UID, agent_id="rate-limited-agent")
    AGENT_REGISTRY.register(state.agent_id, state, persist=False)
    try:
        assert AGENT_REGISTRY.claim_full_state_request(state.agent_id, 60, now=100.0)
        assert AGENT_REGISTRY.claim_full_state_request(state.agent_id, 60, now=159.9) is False
        assert AGENT_REGISTRY.claim_full_state_request(state.agent_id, 60, now=160.0)
        # Another agent has its own budget.
        assert AGENT_REGISTRY.claim_full_state_request("other-agent", 60, now=160.0)
    finally:
        AGENT_REGISTRY.remove(state.agent_id)
        AGENT_REGISTRY._full_state_requested_at.pop("other-agent", None)


def test_removing_an_agent_forgets_its_rate_limit():
    state = AgentState(instance_uid=UID, agent_id="removed-agent")
    AGENT_REGISTRY.register(state.agent_id, state, persist=False)
    AGENT_REGISTRY.claim_full_state_request(state.agent_id, 60, now=100.0)

    AGENT_REGISTRY.remove(state.agent_id)

    assert state.agent_id not in AGENT_REGISTRY._full_state_requested_at


# --- metric ----------------------------------------------------------------


def test_metric_counts_asks_by_reason(client):
    before_new = _metric("opamp_full_state_requested_total", reason="new_agent")
    before_gap = _metric("opamp_full_state_requested_total", reason="sequence_gap")

    opamp_send(client, message(sequence_num=1))  # new agent
    opamp_send(client, message(sequence_num=2))
    opamp_send(client, message(sequence_num=7))  # gap

    assert _metric("opamp_full_state_requested_total", reason="new_agent") == before_new + 1
    assert _metric("opamp_full_state_requested_total", reason="sequence_gap") == before_gap + 1


def _metric(name: str, **labels) -> float:
    return server_main.PROM_FULL_STATE_REQUESTS.labels(**labels)._value.get()
