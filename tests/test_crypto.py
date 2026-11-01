"""Encryption at rest for config bodies (#98).

The acceptance criteria this file exists for:

* a database file inspected with `sqlite3` contains no sentinel secret when
  encryption is enabled;
* starting with config push on and no key fails unless the plaintext opt-out is
  set, and that opt-out is reported in `/health`;
* key rotation re-encrypts existing rows, and old-key rows fail closed with a
  clear error.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from server import crypto
from server.state import AgentState, SQLiteAgentStore, SQLiteDesiredConfigStore

SECRET = "s3cr3t-do-not-store-me"
KEY = crypto.generate_key()
AAD = "agent:" + "01" * 16


@pytest.fixture(autouse=True)
def _clean_keys(monkeypatch):
    for var in (crypto.KEY_ENV, crypto.PREVIOUS_KEY_ENV, crypto.ALLOW_PLAINTEXT_ENV):
        monkeypatch.delenv(var, raising=False)
    crypto.reset_key_cache()
    yield
    crypto.reset_key_cache()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(crypto.KEY_ENV, KEY)
    crypto.reset_key_cache()
    return KEY


# ------------------------------------------------------------------ key handling


def test_generate_key_is_32_base64_bytes():
    import base64

    assert len(base64.b64decode(crypto.generate_key())) == 32


def test_unconfigured_means_no_key(keyed):
    assert crypto.configured() is True
    assert crypto.at_rest_mode() == "encrypted"


def test_no_key_and_no_opt_out_is_unconfigured():
    assert crypto.configured() is False
    assert crypto.at_rest_mode() == "unconfigured"


def test_plaintext_opt_out_is_reported_as_plaintext(monkeypatch):
    monkeypatch.setenv(crypto.ALLOW_PLAINTEXT_ENV, "true")
    assert crypto.at_rest_mode() == "plaintext"
    assert crypto.describe()["plaintext_opt_out"] is True


@pytest.mark.parametrize("bad", ["not-base64!!!", "c2hvcnQ=", ""])
def test_unusable_keys_are_rejected_loudly(monkeypatch, bad):
    monkeypatch.setenv(crypto.KEY_ENV, bad)
    crypto.reset_key_cache()
    if not bad:
        assert crypto.configured() is False  # empty = unset, not an error
        return
    with pytest.raises(crypto.ConfigKeyError):
        crypto.configured()


def test_key_id_is_stable_and_short(keyed):
    key = crypto.current_key()
    assert key is not None
    assert len(crypto.key_id(key)) == crypto.KEY_ID_CHARS
    assert crypto.key_id(key) == crypto.key_id(key)


# ---------------------------------------------------------------- seal / open


def test_seal_open_round_trip(keyed):
    envelope = crypto.seal(SECRET, AAD)
    assert envelope.startswith("enc:")
    assert SECRET not in envelope
    assert crypto.open_sealed(envelope, AAD) == SECRET


def test_sealing_is_randomised(keyed):
    """Two seals of the same body must differ — otherwise a nonce is being reused."""
    assert crypto.seal(SECRET, AAD) != crypto.seal(SECRET, AAD)


def test_wrong_aad_fails_closed(keyed):
    envelope = crypto.seal(SECRET, AAD)
    with pytest.raises(crypto.ConfigDecryptError) as exc:
        crypto.open_sealed(envelope, "agent:" + "02" * 16)
    assert "authentication" in str(exc.value)


def test_a_copied_envelope_does_not_decrypt_for_another_row(keyed):
    """AAD binding: moving a ciphertext between rows must not work."""
    other = "revision:rev-2"
    envelope = crypto.seal(SECRET, AAD)
    with pytest.raises(crypto.ConfigDecryptError):
        crypto.open_sealed(envelope, other)


def test_tampered_ciphertext_fails_closed(keyed):
    envelope = crypto.seal(SECRET, AAD)
    head, key_id, payload = envelope.split(":", 2)
    flipped = payload[:-4] + ("AAAA" if payload[-4:] != "AAAA" else "BBBB")
    with pytest.raises(crypto.ConfigDecryptError):
        crypto.open_sealed(f"{head}:{key_id}:{flipped}", AAD)


def test_missing_key_fails_with_a_clear_error(keyed, monkeypatch):
    envelope = crypto.seal(SECRET, AAD)
    monkeypatch.delenv(crypto.KEY_ENV)
    crypto.reset_key_cache()
    with pytest.raises(crypto.ConfigDecryptError) as exc:
        crypto.open_sealed(envelope, AAD)
    assert "neither the current" in str(exc.value)


def test_malformed_envelopes_are_rejected(keyed):
    with pytest.raises(crypto.ConfigDecryptError):
        crypto.open_sealed("enc:v1:not-base64!!!", AAD)
    with pytest.raises(crypto.ConfigDecryptError):
        crypto.open_sealed("plain text", AAD)


def test_seal_if_configured_passes_through_without_a_key():
    assert crypto.seal_if_configured(SECRET, AAD) == SECRET
    assert crypto.open_if_sealed(SECRET, AAD) == SECRET


def test_seal_if_configured_is_idempotent(keyed):
    once = crypto.seal_if_configured(SECRET, AAD)
    assert crypto.seal_if_configured(once, AAD) == once


# -------------------------------------------------------------------- rotation


def test_needs_rotation_and_rotate(keyed, monkeypatch):
    old_envelope = crypto.seal(SECRET, AAD)
    assert crypto.needs_rotation(old_envelope) is False

    new_key = crypto.generate_key()
    monkeypatch.setenv(crypto.PREVIOUS_KEY_ENV, KEY)
    monkeypatch.setenv(crypto.KEY_ENV, new_key)
    crypto.reset_key_cache()

    # Readable while the previous key is still supplied...
    assert crypto.open_sealed(old_envelope, AAD) == SECRET
    assert crypto.needs_rotation(old_envelope) is True

    rotated = crypto.rotate_sealed(old_envelope, AAD)
    assert crypto.needs_rotation(rotated) is False
    assert crypto.open_sealed(rotated, AAD) == SECRET

    # ...and once the previous key is dropped, the old row fails closed.
    monkeypatch.delenv(crypto.PREVIOUS_KEY_ENV)
    crypto.reset_key_cache()
    with pytest.raises(crypto.ConfigDecryptError):
        crypto.open_sealed(old_envelope, AAD)


# --------------------------------------------------------------- at rest (DB)


def _effective_config():
    return json.dumps(
        {"exporters": {"otlphttp": {"endpoint": "https://x", "password": SECRET}}}
    )


def test_agent_row_is_sealed_on_disk(keyed, tmp_path):
    db = tmp_path / "agents.db"
    store = SQLiteAgentStore(db)
    state = AgentState(
        instance_uid=b"\x01" * 16,
        agent_id="01" * 16,
        effective_config=_effective_config(),
    )
    store.upsert(state)

    # The acceptance criterion: `sqlite3` sees no sentinel secret.
    with sqlite3.connect(db) as conn:
        raw = conn.execute("SELECT effective_config FROM agents").fetchone()[0]
    assert SECRET not in raw
    assert raw.startswith("enc:")

    # ...and the app still reads it back.
    loaded = store.load_all()["01" * 16]
    assert SECRET in loaded.effective_config
    assert loaded.config_error is None


def test_plaintext_mode_writes_plaintext(tmp_path):
    db = tmp_path / "agents.db"
    store = SQLiteAgentStore(db)
    store.upsert(
        AgentState(
            instance_uid=b"\x01" * 16,
            agent_id="01" * 16,
            effective_config=_effective_config(),
        )
    )
    with sqlite3.connect(db) as conn:
        raw = conn.execute("SELECT effective_config FROM agents").fetchone()[0]
    assert SECRET in raw  # documented, explicit opt-out only


def test_pre_98_plaintext_rows_still_load(keyed, tmp_path):
    db = tmp_path / "agents.db"
    store = SQLiteAgentStore(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, instance_uid, connected_at, last_heartbeat, "
            "effective_config, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("01" * 16, b"\x01" * 16, "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00", _effective_config(), "2026-01-01T00:00:00+00:00"),
        )
    loaded = store.load_all()["01" * 16]
    assert SECRET in loaded.effective_config


def test_undecryptable_row_fails_closed_without_data_loss(keyed, tmp_path, monkeypatch):
    db = tmp_path / "agents.db"
    store = SQLiteAgentStore(db)
    store.upsert(
        AgentState(
            instance_uid=b"\x01" * 16,
            agent_id="01" * 16,
            effective_config=_effective_config(),
        )
    )

    # A process started with a different key.
    monkeypatch.setenv(crypto.KEY_ENV, crypto.generate_key())
    crypto.reset_key_cache()
    loaded = store.load_all()["01" * 16]
    assert loaded.effective_config is None
    assert "neither the current" in loaded.config_error

    # Re-persisting must not replace the ciphertext with NULL: supplying the right
    # key later has to find the row still there.
    store.upsert(loaded)
    with sqlite3.connect(db) as conn:
        raw = conn.execute("SELECT effective_config FROM agents").fetchone()[0]
    assert raw is not None and raw.startswith("enc:")

    monkeypatch.setenv(crypto.KEY_ENV, KEY)
    crypto.reset_key_cache()
    assert SECRET in store.load_all()["01" * 16].effective_config


# ------------------------------------------------------- desired config store


def test_desired_config_blob_is_sealed_and_prunable(keyed, tmp_path):
    db = tmp_path / "config.db"
    store = SQLiteDesiredConfigStore(db)
    store.put("01" * 16, "rev-1", {"": {"body": f"password: {SECRET}"}}, "ab" * 32, "alice")
    store.put("01" * 16, "rev-2", {"": {"body": "ok: true"}}, "cd" * 32, "alice")

    with sqlite3.connect(db) as conn:
        blobs = [row[0] for row in conn.execute("SELECT config_blob FROM config_revisions")]
    assert all(blob.startswith("enc:") for blob in blobs)
    assert SECRET not in "".join(blobs)

    assert store.current("01" * 16)["revision_id"] == "rev-2"
    assert store.previous("01" * 16)["revision_id"] == "rev-1"

    # Retention drops the superseded body, never the current one.
    pruned = store.prune("2999-01-01T00:00:00+00:00")
    assert pruned == 1
    assert store.current("01" * 16)["config_blob"] is not None
    old = store.revision("rev-1")
    assert old["config_blob"] is None
    assert old["config_hash"] == "ab" * 32  # hash and metadata survive
    assert old["body_pruned_at"]
