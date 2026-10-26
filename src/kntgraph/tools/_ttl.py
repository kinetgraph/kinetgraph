# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Read a tool request's age from the queued payload (ADR-085).

A tool's latency budget is a property of the **tool**: it is how
long the tool may legitimately take. The ``WorkerManager`` knows
the tools, so it holds that declaration, and the reaper — which
runs inside the manager — applies it.

The deadline is therefore **derived**, not published:

    requested_at + timeout − now

``requested_at`` is the request event's own ``timestamp``, which
travels in the queued payload. Nothing has to be handed to
anybody, so no wiring exists to get wrong and the router stays
blind to execution time.

## The contract

The reader receives the **stream entry's field map** — the shape
``XRANGE`` returns: ``{b"payload": <wire json>}``. The wire
payload is the JSON form of a ``tool.<name>.requested`` event,
whose ``timestamp`` is when the request was made.

The reader is deliberately narrow. A reaper that guesses at
payload shapes is a reaper that can mis-read a deadline, so the
shapes it accepts are enumerated and pinned by
``tests/unit/tools/test_ttl.py``.

Three decisions the reader makes:

1. **The request's age comes from the event's ``timestamp``**,
   next to the tool's own parameters.
2. **A missing or unusable timestamp returns ``None``.** The
   caller falls back to the global ``reaper_idle_time``. The
   reader never invents a value and never raises: a reader that
   raises would take the reaper down; one that guesses would
   reclaim a message early.
3. **An expired deadline yields ``0``** — reclaimable
   immediately.

Why a separate module: the reading is a pure function over a
wire payload — no Redis dependency, no ``self`` — so it is
testable in isolation and keeps the Manager's cyclomatic
complexity under the gate's ceiling.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

# The key under which a stream entry carries the wire payload.
_PAYLOAD_KEY = "payload"

# The event field that records when the request was made.
_TIMESTAMP_KEY = "timestamp"


def _as_mapping(value: Any) -> dict[str, Any] | None:
    """Return ``value`` as a mapping, or ``None``.

    The wire payload may arrive as bytes (the stream's raw
    field), as a string, or already decoded.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(value, str):
        text = value
    else:
        return None
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _unwrap_event(payload: Any) -> dict[str, Any] | None:
    """Return the request's event envelope, or ``None``.

    Accepts the stream entry's field map (the wire payload lives
    under ``payload``) or the decoded event envelope itself.
    """
    mapping = _as_mapping(payload)
    if mapping is None:
        return None
    # A stream entry's field map.
    for key in (_PAYLOAD_KEY, _PAYLOAD_KEY.encode()):
        if key in mapping:
            nested = _unwrap_event(mapping[key])
            if nested is not None:
                return nested
    # A decoded event envelope carries ``timestamp``.
    if _TIMESTAMP_KEY in mapping:
        return mapping
    return None


def read_requested_at(payload: Any) -> datetime | None:
    """When the request was made, or ``None``.

    The request event's own ``timestamp`` is the moment the
    request was issued. It travels in the queued payload, so the
    reaper can derive the deadline from it plus the tool's
    declared timeout — no deadline has to be published to
    anybody.

    ``None`` means "this payload carries no usable timestamp" —
    the caller falls back to the global ``reaper_idle_time``.
    """
    envelope = _unwrap_event(payload)
    if envelope is None:
        return None
    stamp = envelope.get(_TIMESTAMP_KEY)
    if stamp is None:
        return None
    try:
        return datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None


__all__ = [
    "read_requested_at",
]
