"""The single audit seam for security-relevant decisions (#95, #97).

Every privileged decision in this server — an agent credential refused, an admin
login that failed, a session revoked, a principal created — emits exactly one line
here and nothing else. #97 replaces this sink with an append-only, hash-chained
table; keeping the *call site* in one function is what makes that a change in one
file instead of a hunt through `server/main.py`.

Two rules, both load-bearing:

1. **Never pass a secret.** Callers pass identifiers (`principal`, `claimed`,
   `client`), never a token, token hash or password. `_FORBIDDEN_KEYS` is a belt:
   a field named like a credential is dropped rather than logged.
2. **Never raise.** An audit bug may not take the OpAMP endpoint or the admin API
   down — a denied request that 500s is an availability incident caused by
   bookkeeping.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from loguru import logger

# Field names that would mean a secret leaked into a log line. Dropped, not
# truncated: a partially logged token is still a leaked token.
_FORBIDDEN_KEYS = frozenset(
    {
        "token",
        "token_hash",
        "secret",
        "secret_hash",
        "password",
        "password_hash",
        "authorization",
        "hash",
    }
)


def audit_event(event: str, **fields: Any) -> None:
    """Write one structured `AUDIT {json}` line. Never raises, never logs secrets."""
    payload: Dict[str, Any] = {"event": event}
    payload.update(
        {
            key: value
            for key, value in fields.items()
            if value not in (None, "") and key.lower() not in _FORBIDDEN_KEYS
        }
    )
    try:
        logger.warning("AUDIT " + json.dumps(payload, default=str, sort_keys=True))
    except Exception:  # pragma: no cover - defensive
        logger.warning(f"AUDIT {event}")
