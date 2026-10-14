# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Redis tool-queue keys.

Single source of truth for the wire format of the per-tool
Stream key written by :class:`ToolRouter` and read by
:class:`WorkerManager`:

  - :data:`TOOL_QUEUE_KEY_TEMPLATE` -- suffix template,
    ``"knt:tools:{tool_name}:queue"``.
  - :func:`tool_queue_key` -- compose the prefix with the
    template so the full Redis key is
    ``namespaced(prefix, "knt:tools:{tool_name}:queue")``.
  - :data:`TOOL_EVENT_ID_INDEX` -- suffix template for the
    dedup index, ``"knt:tools:{tool_name}:idem:{event_id}"``.
  - :func:`tool_event_id_key` -- compose the prefix with
    the dedup template (ADR-083).
  - :data:`DEDUP_TTL_SECONDS_DEFAULT` -- default TTL for
    the dedup index (1 hour; ADR-083 §3.1 rationale).

ADR-076 -- namespace prefix
---------------------------

The storage side (EventLog, DLQ, memory tiers,
``WorldCheckpointStorage``) was wired with
``Settings.redis_key_prefix`` in v0.16.0. The dispatcher
side (``WorkerManager``, ``ToolRouter``, the
``stuck_in_queue`` query inside :class:`ReactiveDispatcher`)
was not, leaving two services sharing one Redis still
cross-talking at the tool-queue boundary (DEBT §2.35).

This module is the single point of truth for the
tool-queue wire format. The dispatcher side composes
the prefix at every key build via ``namespaced``,
mirroring the pattern in
:mod:`kntgraph.infra.redis._event_log._keys`.

ADR-083 -- tool-queue dedup index
---------------------------------

The ``ToolRouter`` dedup-by-``event_id`` feature
(ADR-083, opt-in via ``dedup=True``) consults a
SET-based idempotency index before every ``XADD``. The
index key (``TOOL_EVENT_ID_INDEX``) lives in a separate
Redis namespace from the EventLog dedup index
(``EVENT_ID_INDEX`` in :mod:`kntgraph.infra.redis._event_log._keys`)
so the two do not collide under a shared Redis (the
multi-service isolation case from ADR-076).
"""

from __future__ import annotations

from kntgraph.infra.redis._prefix import namespaced

# Suffix template. Pure string; the adapter prepends the
# prefix via :func:`namespaced` at every key build.
TOOL_QUEUE_KEY_TEMPLATE: str = "knt:tools:{tool_name}:queue"
"""Per-tool Redis Stream key (suffix template)."""

TOOL_EVENT_ID_INDEX: str = "knt:tools:{tool_name}:idem:{event_id}"
"""Per-``(tool_name, event_id)`` SET-based idempotency index.

Maps ``event_id`` to the stream_id of the first ``XADD`` for
that event. Written by
:func:`kntgraph.infra.redis._tools._idempotency.claim_tool_queue_slot`
(ADR-083). The key lives in a different Redis namespace
from the EventLog dedup index (``knt:eventids:<id>``) so
the two do not collide under a shared Redis.
"""

DEDUP_TTL_SECONDS_DEFAULT: int = 3_600
"""Default TTL (1 hour) for the tool-queue dedup index.

Bounds the index size per tool. Long enough to cover the
re-emit window of any sane reactive dispatcher (the
backoffice 2026-10-14 incident replays on a 1-2 minute
cadence); short enough to bound Redis memory.

The EventLog uses ``IDEMPOTENCY_TTL_DEFAULT = 86_400`` (24h)
because agent-side event replays span longer sagas. Tool
queues are per-request; 1h is the right shape.
"""


def tool_queue_key(prefix: str, tool_name: str) -> str:
    """Build the Stream key for ``tool_name`` under ``prefix``.

    Empty ``prefix`` returns the unprefixed key
    (``"knt:tools:{tool_name}:queue"``) -- byte-for-byte
    identical to the pre-ADR-076 wire format. Non-empty
    ``prefix`` concatenates the prefix in front, producing
    e.g. ``"acme-billing:knt:tools:echo:queue"``.

    The composition is a plain string concatenation -- no
    separator is inserted between the prefix and the
    suffix. Operators who want a trailing colon write
    ``"acme-billing:"`` (note the colon).
    """
    return namespaced(prefix, TOOL_QUEUE_KEY_TEMPLATE.format(tool_name=tool_name))


def tool_event_id_key(prefix: str, tool_name: str, event_id: str) -> str:
    """Build the dedup-index key for ``(tool_name, event_id)`` under ``prefix``.

    Empty ``prefix`` returns the unprefixed key
    (``"knt:tools:echo:idem:<uuid>"``); non-empty prefix
    composes via :func:`namespaced` so the multi-service
    isolation from ADR-076 holds for the dedup index too.

    ADR-083 §3.1: the key is in a different Redis namespace
    from the EventLog dedup index so a shared Redis does
    not let a tool-queue dedup entry collide with an
    EventLog dedup entry.
    """
    return namespaced(
        prefix, TOOL_EVENT_ID_INDEX.format(tool_name=tool_name, event_id=event_id)
    )


__all__ = [
    "DEDUP_TTL_SECONDS_DEFAULT",
    "TOOL_EVENT_ID_INDEX",
    "TOOL_QUEUE_KEY_TEMPLATE",
    "tool_event_id_key",
    "tool_queue_key",
]
