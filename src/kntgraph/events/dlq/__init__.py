# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Dead Letter Queue for the v2.0 event model.

When a system (cyclic or reactive) cannot process an event, the
event is sent to the DLQ with a `DLQReason` and an error message.
The DLQ is itself a Redis Stream:

    knt:dlq:events

Indexed by:

    HASH  knt:dlq:reasons         = { reason: count }   (stats)
    HASH  knt:dlq:by_agent        = { agent_id: stream_id }  (lookup)
    HASH  knt:dlq:by_event_id     = { event_id:  stream_id } (idempotency)

DLQ entries are also idempotent: a second failure of the same
event (same event_id, same reason) is a no-op.

Reprocessing
------------
`reprocess(event_id)` returns the original Event so the caller can
re-append it to the EventLog. Since the EventLog is idempotent,
re-appending an event whose id is already in the log is a no-op;
the reprocess step is what re-arms the event for fresh processing
if the underlying transient issue is fixed.

This module is a thin facade. The implementation is split across
the `dlq` subpackage:

  - `dlq.values`     — `DLQReason` enum, `DeadLetterEvent`
    dataclass (with to_dict / from_dict), and the four
    Redis key constants.
  - `dlq.store`      — `DeadLetterQueue` class: Redis-backed
    storage + read (append, get_event, list_for_agent,
    list_by_reason, list_all, get_stats, purge).
  - `dlq.actions`    — `DeadLetterActions` (subclasses
    `DeadLetterQueue`): the high-level API (`reprocess`,
    `discard`).
"""

from .actions import DeadLetterActions
from .store import DeadLetterQueue
from .values import (
    DLQ_AGENT_INDEX,
    DLQ_EVENT_INDEX,
    DLQ_REASON_INDEX,
    DLQ_STREAM_KEY,
    DLQReason,
    DeadLetterEvent,
)

__all__ = [
    "DLQ_AGENT_INDEX",
    "DLQ_EVENT_INDEX",
    "DLQ_REASON_INDEX",
    "DLQ_STREAM_KEY",
    "DLQReason",
    "DeadLetterActions",
    "DeadLetterEvent",
    "DeadLetterQueue",
]
