"""Detecting an incomplete status report (#70).

Spec §Agent Status Compression (`specification.md:1233-1279`): an agent MAY omit
the sub-messages that have not changed since it last reported them. That means an
`AgentToServer` is not necessarily a full picture of the agent, and the *only*
signal that the server missed something is `sequence_num`:

    "When the Server receives an AgentToServer message sequence_num field value
     that is not exactly by one greater than the previously received
     sequence_num value then the Server knows it does not have full status"

The recovery the spec mandates is a `ServerToAgent` with `ReportFullState` set.

Two decisions live here rather than in the receive path, because both are easy to
get subtly wrong and both are pinned by tests:

* **`0` means "unknown", not "first message".** Not every SDK sets the field, and
  treating 0 as a value would make an agent that never sets it look like it was
  stuck on one number forever — a `ReportFullState` request on every single
  message, which is a ping-pong that costs more than the compression saves.
* **Wrap-safe arithmetic.** `sequence_num` is an unsigned 32-bit field that wraps.
  The test is `(current - previous) & 0xFFFFFFFF == 1`, never `current > previous`:
  the latter breaks at the wrap and cannot express "a decrease is also a gap".

Deliberately *not* a gap signal: an absent field group. The spec's compression
model makes a bare heartbeat from an agent we already know everything about
completely normal, so "this message omits `health` and we have health stored" is
indistinguishable from correct compression. Flagging it would request full state
from every well-behaved agent on every poll. `sequence_num` is the signal the spec
provides; use it alone.
"""

from __future__ import annotations

from typing import Optional

SEQUENCE_NUM_MASK = 0xFFFFFFFF


def is_gap(previous: Optional[int], current: int) -> bool:
    """True if `current` is not exactly one greater than `previous`.

    `previous` is `None` for an agent we have no baseline for yet (first contact
    already asks for full state, so there is nothing to repair). `current == 0`
    means the field was not set, which is not evidence of anything. A *decrease*
    (an agent that restarted and reset its counter) is a gap, because the status
    we hold describes a counter the agent no longer has.
    """
    if not current:
        return False
    if not previous:
        return False
    return ((current - previous) & SEQUENCE_NUM_MASK) != 1
