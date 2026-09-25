# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Redis key conventions for the EventLog.

Single source of truth for the wire format:

  - ``stream_key_for_agent(agent_id)`` -- per-agent Stream key
  - ``event_id_key(event_id)``         -- SET-based idempotency key
  - ``scan_pattern(prefix)``           -- glob for listing agents
  - ``parse_agent_id_from_stream_key`` -- inverse of ``stream_key_for_agent``
  - ``MAXLEN_DEFAULT``                 -- per-stream trim threshold

The previous location was ``stream/event_log/store.py``
(``AGENT_STREAM_KEY``, ``EVENT_ID_INDEX``). Moving the
constants here lets the storage adapter own the wire format
without leaking it back into ``stream/event_log``.

ADR-076 -- namespace prefix
--------------------------

The keys below are the canonical suffix templates. The
storage adapter composes them with the namespace prefix
(``Settings.redis_key_prefix``) at every ``_k()`` call:

    prefix=""            key="knt:agents:a-1:events"
        => "knt:agents:a-1:events"  (no change, pre-076)

    prefix="acme-billing:" key="knt:agents:a-1:events"
        => "acme-billing:knt:agents:a-1:events"

The ``SCAN_PATTERN`` constant is replaced by the
``scan_pattern(prefix)`` function so the glob picks up
the same prefix the writer used. The
``parse_agent_id_from_stream_key`` parser also takes
the prefix so it strips the right slice off.
"""

from __future__ import annotations

from kntgraph.infra.redis._prefix import namespaced


# Suffix templates. Pure strings; the adapter prepends the
# prefix via :func:`namespaced` at every key build.
AGENT_STREAM_KEY: str = "knt:agents:{agent_id}:events"
"""Per-agent event log Redis Stream key (suffix template)."""

EVENT_ID_INDEX: str = "knt:eventids:{event_id}"
"""SET-based idempotency index. Maps event_id -> stream_id."""

MAXLEN_DEFAULT: int = 100_000
"""Default per-stream MAXLEN (auto-trim). Override per adapter."""

IDEMPOTENCY_TTL_DEFAULT: int = 86_400
"""Default TTL in seconds for the event_id idempotency index (24 hours)."""


# The bare glob pattern (prefix=""). Kept as a module
# constant for callers that need the canonical no-prefix
# shape (e.g. documentation, debug commands).
SCAN_PATTERN: str = "knt:agents:*:events"
"""Glob pattern for ``scan_iter`` when no prefix is set."""


def stream_key_for_agent(prefix: str, agent_id: str) -> str:
    """Build the stream key for an agent under ``prefix``."""
    return namespaced(prefix, AGENT_STREAM_KEY.format(agent_id=agent_id))


def event_id_key(prefix: str, event_id: str) -> str:
    """Build the idempotency key for an event_id under ``prefix``."""
    return namespaced(prefix, EVENT_ID_INDEX.format(event_id=event_id))


def scan_pattern(prefix: str) -> str:
    """Glob pattern for ``scan_iter`` under ``prefix``.

    Composes the same suffix as :func:`stream_key_for_agent`
    but with ``*`` in place of the agent_id so the SCAN can
    enumerate every stream the writer created.
    """
    return namespaced(prefix, "knt:agents:*:events")


def parse_agent_id_from_stream_key(key: str, prefix: str = "") -> str | None:
    """Extract the agent_id portion of a stream key written
    under ``prefix``.

    Returns ``None`` if the key does not match the expected
    pattern (e.g. a key from a different prefix). Used by
    :meth:`RedisEventLogAdapter.list_agents` to decode the
    result of ``scan_iter``.

    The default ``prefix=""`` preserves the pre-ADR-076
    parser (matches keys written without a prefix). When
    ``prefix`` is non-empty, the prefix slice is stripped
    before the substring match.
    """
    base_prefix = namespaced(prefix, "knt:agents:")
    suffix = ":events"
    if key.startswith(base_prefix) and key.endswith(suffix):
        return key[len(base_prefix) : -len(suffix)]
    return None


__all__ = [
    "AGENT_STREAM_KEY",
    "EVENT_ID_INDEX",
    "IDEMPOTENCY_TTL_DEFAULT",
    "MAXLEN_DEFAULT",
    "SCAN_PATTERN",
    "event_id_key",
    "parse_agent_id_from_stream_key",
    "scan_pattern",
    "stream_key_for_agent",
]
