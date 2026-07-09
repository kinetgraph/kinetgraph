# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
EventLog — append-only, per-agent, idempotent event log on Redis Streams.

Layout
------
Each agent has its own stream:

    knt:agents:{agent_id}:events

Events are serialized as Redis Stream entries. The `event_id` UUID
(generated deterministically) is stored as a secondary index entry
to enable idempotent appends:

    knt:eventids:{event_id}  = stream_entry_id

A new event is appended only if `event_id` is not yet present. This
guarantees that re-running a system on the same world (replay) does
NOT duplicate events in the stream.

The default projection (Redis Streams) is the source of truth. The
World is a derived value obtained by folding the events.

Idempotency protocol
--------------------
1. Caller computes event_id deterministically (already done by
   Event.create with the right causation_id).
2. Caller calls `await log.append(event)`.
3. Log checks if `knt:eventids:{event_id}` exists.
4. If yes → no-op (returns the existing stream entry id).
5. If no → XADD + SET eventid key, atomically via MULTI/EXEC.

This module is a thin facade. The implementation is split
across the `event_log` subpackage:

  - `event_log.store`     — the `EventLog` class (Redis-backed
    CRUD + the 4-stage `append` pipeline).
  - `event_log.validation`— pre-flight guards
    (`validate_agent_id_for_redis`, `check_signature`,
    `check_tenant_ownership`).
  - `event_log.dispatch`   — resilience layer
    (`dispatch_redis_call`).
  - `event_log.codec`      — `event_to_redis`,
    `parse_event` (canonical wire format).
"""

from .codec import event_to_redis, parse_event
from .store import EventLog, _build_default_backoff


# Backwards-compat: AGENT_STREAM_KEY / EVENT_ID_INDEX moved to
# the Redis adapter (ADR-019). Re-export from the new location
# so external callers (tests, fixtures) keep working.
from ...infra.redis import (
    AGENT_STREAM_KEY,
    EVENT_ID_INDEX,
    claim_event_id_slot,
)


# Re-export claim_event_id_slot so test suites that
# patch kntgraph.stream.event_log.claim_event_id_slot
# keep working. The store module imports the symbol
# from the adapter module; re-exporting at the package
# root keeps the public surface stable.
from ...infra.redis._event_log._idempotency import (
    claim_event_id_slot as _claim_event_id_slot,
)

claim_event_id_slot = _claim_event_id_slot  # noqa: F811

# Backwards-compat aliases: the codec functions used to live
# as module-level names (``_event_to_redis``, ``_parse_event``).
# New code should use the codec module directly.
_event_to_redis = event_to_redis
_parse_event = parse_event


__all__ = [
    "AGENT_STREAM_KEY",
    "EVENT_ID_INDEX",
    "EventLog",
    "_build_default_backoff",
    "_event_to_redis",
    "_parse_event",
    "claim_event_id_slot",
    "event_to_redis",
    "parse_event",
]
