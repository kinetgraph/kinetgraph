# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Read the per-message TTL from a queued tool request (ADR-085).

The dispatcher injects ``ttl_remaining`` into the request's
``data`` before routing it to the tool queue (ADR-085 A''). The
reaper reads it back to decide **per message** when a pending
message may be reclaimed: the message's own TTL, not the global
``reaper_idle_time``.

## The contract

The reader receives the **stream entry's field map** — the shape
``XRANGE`` returns: ``{b"payload": <wire json>}``. The wire
payload is the JSON form of a ``tool.<name>.requested`` event,
whose ``data`` carries the TTL.

The reader is deliberately narrow. A reaper that guesses at
payload shapes is a reaper that can mis-read a deadline, so the
shapes it accepts are enumerated and pinned by
``tests/unit/tools/test_ttl.py``.

Three decisions the reader makes:

1. **The TTL lives in the event's ``data``**, next to the tool's
   own parameters — the dispatcher's ``ToolCallTTL`` territory.
2. **A missing or unusable TTL returns ``None``.** The caller
   falls back to the global ``reaper_idle_time``. The reader
   never invents a value and never raises: a reader that
   raises would take the reaper down; one that guesses would
   reclaim a message early.
3. **Zero and negative TTLs are preserved.** They mean "the
   deadline has passed"; the caller clamps them to zero
   (reclaimable immediately). Clamping here would erase the
   distinction between "no TTL" and "expired TTL".

Why a separate module: the reading is a pure function over a
wire payload — no Redis dependency, no ``self`` — so it is
testable in isolation and keeps the Manager's cyclomatic
complexity under the gate's ceiling.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from kntgraph.core.clock import utcnow
from kntgraph.core.event import Event
from kntgraph.core.world.components import ToolCallTTL

# The key the dispatcher writes and the reaper reads.
TTL_REMAINING_KEY = "ttl_remaining"

# The key under which a stream entry carries the wire payload.
_PAYLOAD_KEY = "payload"


def _as_mapping(value: Any) -> dict[str, Any] | None:
    """Return ``value`` as a mapping, or ``None``.

    The wire payload may arrive as bytes (the stream's raw
    field), as a string, or already decoded.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        if isinstance(decoded, dict):
            return decoded
    return None


def _unwrap_data(payload: Any) -> dict[str, Any] | None:
    """Return the request's ``data`` mapping, or ``None``.

    Accepts the stream entry's field map (the wire payload
    lives under ``payload``), the decoded event (the ``data``
    lives under ``data``), or the ``data`` mapping itself.
    """
    mapping = _as_mapping(payload)
    if mapping is None:
        return None
    # A stream entry's field map.
    for key in (_PAYLOAD_KEY, _PAYLOAD_KEY.encode()):
        if key in mapping:
            nested = _unwrap_data(mapping[key])
            if nested is not None:
                return nested
    # A decoded event envelope.
    data = mapping.get("data")
    if isinstance(data, dict):
        return data
    # The ``data`` mapping itself.
    if TTL_REMAINING_KEY in mapping:
        return mapping
    return mapping


def read_ttl_remaining(payload: Any) -> float | None:
    """The message's remaining TTL in seconds, or ``None``.

    ``None`` means "this payload carries no usable TTL" — the
    caller falls back to the global ``reaper_idle_time``. A TTL
    of ``0`` or a negative value means the deadline already
    passed; the caller clamps it to zero (reclaimable
    immediately).
    """
    data = _unwrap_data(payload)
    if data is None:
        return None
    raw = data.get(TTL_REMAINING_KEY)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def remaining_ttl(
    *,
    tool_name: str,
    requested_at: datetime,
    published: Mapping[str, float],
    default: float | None,
) -> float | None:
    """The remaining TTL for a request, in seconds.

    The tool's **declared** timeout wins (``published``); the
    dispatcher's ``default`` is the fallback for a tool that
    declared no opinion. ``None`` (neither) means the request
    carries no TTL and the reaper uses its global
    ``reaper_idle_time``.

    A request that has already waited longer than its budget
    yields ``0.0`` — reclaimable immediately.
    """
    total = published.get(tool_name)
    if total is None:
        if default is None:
            return None
        total = default
    elapsed = (utcnow() - requested_at).total_seconds()
    return max(0.0, total - elapsed)


def with_ttl_remaining(event: Event, ttl_seconds: float) -> str:
    """The request's wire payload, carrying ``ttl_remaining``.

    The TTL lands in the event's ``data``, next to the tool's
    parameters; the worker reads it back with
    :func:`read_ttl_remaining`.
    """
    enriched = dict(event.data)
    enriched[TTL_REMAINING_KEY] = ttl_seconds
    return dataclasses.replace(event, data=enriched).to_json()


def payload_with_ttl(
    *,
    event: Event,
    tool_name: str,
    tool_ttls: ToolCallTTL | None,
    published: Mapping[str, float],
) -> str:
    """The request's wire payload, carrying its remaining TTL.

    The remaining TTL travels with the request so the reaper
    reclaims each message on its own threshold. Without it the
    global ``reaper_idle_time`` reclaims a slow tool while it
    still runs.

    The tool's **declared** timeout wins (``published``);
    ``tool_ttls`` is the fallback for a tool that declared no
    opinion. When neither applies the request carries no TTL:
    the payload is the event's own JSON, byte-for-byte, and the
    reaper falls back to its global default.
    """
    default = None if tool_ttls is None else tool_ttls.ttl_for(tool_name)
    ttl_seconds = remaining_ttl(
        tool_name=tool_name,
        requested_at=event.timestamp,
        published=published,
        default=default,
    )
    if ttl_seconds is None:
        return event.to_json()
    return with_ttl_remaining(event, ttl_seconds)


__all__ = [
    "TTL_REMAINING_KEY",
    "payload_with_ttl",
    "read_ttl_remaining",
    "remaining_ttl",
    "with_ttl_remaining",
]
