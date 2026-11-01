"""The single audit seam: append-only, hash-chained (`server.state.SQLiteAuditStore`).

Every decision worth explaining later — an agent credential refused, an admin login
that failed, a session revoked, a principal created, a config push proposed — emits
exactly one entry here and nothing else. Keeping the *call site* in one function is
what makes the whole server auditable: there is no second way to write the table.

Two entry points, because "never raise" and "fail closed" cannot both be true for
the same call:

``audit_event()``
    **Best effort, never raises.** Used for denials and observability — an agent
    auth failure, a login that was refused, a rate limit. Turning a 401 into a 500
    because bookkeeping failed is a self-inflicted outage, and the structured log
    line is written *first* so the entry survives even when the database does not.

``audit_required()``
    **Fail closed, raises `AuditError`.** Used by privileged mutations. The
    ordering contract is **pre-write, then act**: a caller records the decision
    before performing the mutation, so an action that cannot be recorded does not
    happen (epic #93, T5). A successful action produces exactly one entry; if the
    mutation itself then fails, the caller appends a second entry with
    ``result="error"`` — strictly more information, never less.

Redaction is shared with #98: field names that look like credentials are dropped
rather than truncated (a partially logged token is a still a leaked token), and the
same filter applies to the free-form `detail_json`. #98 extends `scrub_fields()`;
it does not add a second filter.

The best-effort path has a circuit breaker. Without it, a 403 loop or a full disk
turns into one failed SQLite write per request — the audit log becoming the outage
it exists to explain. `available()` reports the breaker, which is what the
config-push posture reads.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import timedelta
from typing import Any, Dict, Optional

from loguru import logger

from server.state import AUDIT_EXPORT_COLUMNS, SQLiteAuditStore, utcnow
from server.redaction import is_secret_key, scrub_fields, scrub_text

# #98 owns the list of secret-shaped key names; #97 only decides what to do with
# them (drop, never truncate). A second list here is how one surface ends up
# unfiltered.
_is_secret_key = is_secret_key

# Free-form detail is capped: an audit row describes what happened, it is not a
# payload dump, and an unbounded `detail_json` is a way to blow up the table.
DETAIL_MAX_CHARS = 4096

# Best-effort covers-mode: N consecutive insert failures, then stop trying for a
# minute. `audit_required` ignores the breaker — a privileged action must still
# fail rather than silently go unrecorded.
BREAKER_THRESHOLD = 5
BREAKER_SECONDS = 60

# One prune per hour at most: `periodic_cleanup` ticks every 15s.
PRUNE_INTERVAL_SECONDS = 3600

# Kinds that identify *what* is speaking, for the `actor_kind` column.
ACTOR_KINDS = ("human", "agent", "system")

# Constructed at import like the other stores; tests swap in a tmp_path-backed one.
STORE = SQLiteAuditStore()


class AuditError(RuntimeError):
    """A privileged action could not be recorded, so it must not proceed."""


# --------------------------------------------------------------------- settings


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def enabled() -> bool:
    """`OPAMP_AUDIT_ENABLED` — on by default.

    Default-on is deliberate: an audit log that an operator has to remember to
    switch on is off in every incident that matters, and the cost is one small row
    per privileged action.
    """
    return _env_flag("OPAMP_AUDIT_ENABLED", True)


def retention_days() -> int:
    """`AUDIT_RETENTION_DAYS` (default 365; `<= 0` means keep forever)."""
    raw = (os.environ.get("AUDIT_RETENTION_DAYS") or "").strip()
    if not raw:
        return 365
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"AUDIT_RETENTION_DAYS={raw!r} is not an integer; using 365")
        return 365


def export_formats() -> tuple:
    return ("jsonl", "csv")


# ---------------------------------------------------------------------- breaker

_failures = 0
_tripped_until = 0.0


def _tripped() -> bool:
    return _tripped_until > time.monotonic()


def _record_failure(exc: Exception) -> None:
    global _failures, _tripped_until
    _failures += 1
    if _failures >= BREAKER_THRESHOLD:
        _tripped_until = time.monotonic() + BREAKER_SECONDS
        logger.error(
            f"audit log unwritable ({_failures} consecutive failures, last: {exc}); "
            f"pausing best-effort audit for {BREAKER_SECONDS}s — entries are still "
            "logged as AUDIT lines"
        )


def _record_success() -> None:
    global _failures
    _failures = 0


def available() -> bool:
    """Is the audit log usable right now? Read by the config-push posture (#96)."""
    return enabled() and not _tripped()


def reset_circuit_breaker() -> None:
    """Clear breaker state (tests, and an operator after fixing permissions)."""
    global _failures, _tripped_until
    _failures = 0
    _tripped_until = 0.0


def breaker_state() -> Dict[str, Any]:
    return {
        "failures": _failures,
        "tripped": _tripped(),
        "retry_in_seconds": max(0, int(_tripped_until - time.monotonic())),
    }


# -------------------------------------------------------------------- redaction
#
# `scrub_fields()` and `is_secret_key()` are `server.redaction`'s (#98): one list
# of secret key names, taught once. They stay importable from here because that is
# where #97's call sites already point.


def _detail_json(fields: Dict[str, Any]) -> Optional[str]:
    """Serialise the free-form part of an entry, redacted and size-capped."""
    clean = {key: value for key, value in fields.items() if value not in (None, "")}
    if not clean:
        return None
    try:
        payload = json.dumps(clean, default=str, sort_keys=True)
    except Exception:  # pragma: no cover - defensive
        payload = json.dumps({"unserialisable": True})
    # Keys were already dropped by `scrub_fields`; this catches a secret *value* in
    # a field whose name looks innocent (`url`, `note`, `endpoint`).
    payload = scrub_text(payload)
    if len(payload) > DETAIL_MAX_CHARS:
        payload = json.dumps(
            {"truncated": True, "preview": payload[: DETAIL_MAX_CHARS // 2]},
            sort_keys=True,
        )
    return payload


# ------------------------------------------------------------------ classifying


def _actor_kind(event: str, principal: str, override: Optional[str]) -> str:
    """Who is speaking: the credential (agent), a person, or the server itself.

    An unauthenticated caller is `human` with actor `anonymous`, not `system`: the
    distinction matters when someone asks ``did the server do this, or did a
    stranger try?``
    """
    if override:
        return override if override in ACTOR_KINDS else "system"
    if event.startswith("agent"):
        return "agent"
    if event.startswith("audit.") or event.startswith("system"):
        return "system"
    return "human"


def _build_entry(event: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Map a call site's keyword soup onto the fixed audit schema.

    Call sites existed before the table did (#95, #94), so the mapping lives here
    rather than forcing thirteen renames: `principal` is the actor, `result` is the
    outcome, and everything else is redacted detail.
    """
    values = dict(fields)
    principal = str(values.pop("principal", "") or "")
    role = values.pop("role", None)
    result = str(values.pop("result", "") or "ok")
    actor_kind = _actor_kind(event, principal, values.pop("actor_kind", None))
    kind = values.pop("kind", None)

    if actor_kind == "agent" and principal:
        actor = f"agent:{principal}"
    elif principal:
        actor = principal
    elif actor_kind == "system":
        actor = "system"
    else:
        actor = "anonymous"

    if kind:
        values["kind"] = kind

    return {
        "ts": utcnow().isoformat(),
        "actor": actor,
        "actor_kind": actor_kind,
        "role": role,
        "action": event,
        "target_kind": values.pop("target_kind", None),
        "target_id": values.pop("target_id", None),
        "revision_id": values.pop("revision_id", None),
        "config_hash": values.pop("config_hash", None),
        "result": result,
        "detail_json": _detail_json(scrub_fields(values)),
    }


def _log_line(event: str, fields: Dict[str, Any]) -> None:
    """The `AUDIT {json}` line: the fallback that survives a dead database."""
    payload: Dict[str, Any] = {"event": event}
    payload.update(
        {
            key: value
            for key, value in fields.items()
            if value not in (None, "") and not _is_secret_key(key)
        }
    )
    try:
        logger.warning(scrub_text("AUDIT " + json.dumps(payload, default=str, sort_keys=True)))
    except Exception:  # pragma: no cover - defensive
        logger.warning(f"AUDIT {event}")


# --------------------------------------------------------------------- the seam


def audit_event(event: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Record one audited decision, best effort. **Never raises.**

    Log line first, database second: if the write fails, the operator still has the
    line, and the breaker stops the next thousand requests from all paying for the
    same broken disk. Returns the stored row, or None when it was only logged.
    """
    _log_line(event, fields)
    if not enabled() or _tripped():
        return None
    try:
        row = STORE.append(_build_entry(event, fields))
    except Exception as exc:  # never raise: a denial that 500s is an outage
        _record_failure(exc)
        return None
    _record_success()
    return row


def audit_required(event: str, **fields: Any) -> Dict[str, Any]:
    """Record one privileged decision or refuse to proceed. Raises `AuditError`.

    Call this **before** the mutation it describes (see the module docstring). It
    ignores the circuit breaker: the point of a privileged entry is that the action
    does not happen without it.
    """
    _log_line(event, fields)
    if not enabled():
        raise AuditError(
            "audit log is disabled (OPAMP_AUDIT_ENABLED=false): refusing a privileged "
            "action that would leave no record"
        )
    try:
        row = STORE.append(_build_entry(event, fields))
    except Exception as exc:
        _record_failure(exc)
        raise AuditError(f"could not record '{event}': {exc}") from exc
    _record_success()
    return row


def preflight() -> None:
    """Fail closed before a route body runs, without writing an entry.

    Used by `server.auth.require_audit` so a privileged route cannot start work it
    will then be unable to record. Cheap: the breaker is process state, not a query.
    """
    if not enabled():
        raise AuditError(
            "audit log is disabled (OPAMP_AUDIT_ENABLED=false): refusing a privileged "
            "action that would leave no record"
        )


# --------------------------------------------------------------------- exporting


def format_entries(rows, format: str = "jsonl") -> str:
    """Render audit rows as `jsonl` or `csv`, oldest first (callers pass the order).

    One implementation for the API, the CLI and the offline (`--db`) path: an
    export format that differs per surface is an export nobody can diff.
    """
    columns = list(AUDIT_EXPORT_COLUMNS)
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()
    return "".join(
        json.dumps({key: row.get(key) for key in columns}, default=str) + "\n"
        for row in rows
    )


# --------------------------------------------------------------------- retention

_last_prune = 0.0


def maybe_prune() -> int:
    """Delete entries past `AUDIT_RETENTION_DAYS`, at most once an hour.

    The prune leaves an `audit.checkpoint` row recording the hash it cut to, which
    is what keeps `GET /audit/verify` from reporting a permanent break. A retention
    window is a policy, not tampering; deleting rows *before* the checkpoint still
    shows up.
    """
    global _last_prune
    days = retention_days()
    if days <= 0:
        return 0
    now = time.monotonic()
    if _last_prune and now - _last_prune < PRUNE_INTERVAL_SECONDS:
        return 0
    _last_prune = now
    cutoff = (utcnow() - timedelta(days=days)).isoformat()
    try:
        pruned = STORE.prune(cutoff)
    except Exception as exc:
        logger.warning(f"audit retention prune failed: {exc}")
        return 0
    if pruned:
        logger.info(f"audit retention: pruned {pruned} entries older than {cutoff}")
    return pruned


def reset_prune_timer() -> None:
    """Allow the next `maybe_prune()` to run (tests)."""
    global _last_prune
    _last_prune = 0.0
