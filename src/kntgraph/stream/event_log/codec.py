# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event_log.codec -- Wire format for `Event`.

Two pure functions:

  - `event_to_redis(event)`: serialise an `Event`
    as the dict that goes into a Redis Stream entry.
    All values are strings; structured values (data,
    correlation) are JSON-encoded.

  - `parse_event(_stream_id, mdata)`: inverse —
    reconstruct an `Event` from a Redis Stream entry.

The `causation_id` field is taken from
`event.causation_id` (the event-level explicit causal
parent). If absent, it falls back to
`event.correlation.causation_id` for
backward-compatibility with events emitted before the
field was promoted.

The `signature` field (ADR-016 L1) is added as a
JSON-encoded string when present, or empty string when
absent. The `parse_event` path decodes a corrupted
signature defensively (treats as absent so downstream
`verify_event` sees `signature=None` and returns False).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ...core.event import Event
from ...infra.redis._codec import decode_value


def event_to_redis(event: Event) -> dict[str, Any]:
    """
    Serialize an Event as a Redis Stream entry. All values are
    strings; structured values (data, correlation) are JSON-encoded.

    `causation_id` is taken from `event.causation_id` (the
    event-level explicit causal parent). If absent, falls
    back to `event.correlation.causation_id` for
    backward-compatibility with events emitted before the
    field was promoted.

    `signature` (ADR-016 L1) is added as a JSON-encoded string
    when present, or empty string when absent. This is
    additive: existing entries without ``signature`` decode
    back to ``event.signature=None``; new entries carry the
    signature and a verifier can replay the JCS-canonical
    bytes to confirm authenticity.
    """
    cid = event.causation_id or event.correlation.causation_id
    payload: dict[str, Any] = {
        "event_id": str(event.event_id),
        "agent_id": event.agent_id,
        "event_type": event.event_type,
        "event_class": event.event_class,
        "timestamp": event.timestamp.isoformat(),
        "version": str(event.version),
        "data": json.dumps(dict(event.data), default=str, sort_keys=True),
        "correlation_id": str(event.correlation.correlation_id),
        "causation_id": str(cid) if cid else "",
        "span_id": str(event.correlation.span_id) if event.correlation.span_id else "",
        "metadata": json.dumps(
            dict(event.correlation.metadata), default=str, sort_keys=True
        ),
        # ADR-016 PR 3: signature wire format. Empty string
        # when the producer did not sign; JSON object when
        # it did. Empty string is forward-compatible with
        # Redis Streams (a "" value is harmless).
        "signature": (
            json.dumps(event.signature.to_dict(), sort_keys=True)
            if event.signature is not None
            else ""
        ),
    }
    return payload


def parse_event(_stream_id: bytes, mdata: dict) -> Event:
    """
    Inverse of `event_to_redis`. Reads from a Redis Stream entry
    (bytes) and reconstructs an Event.

    Delegates to ``Event.from_dict`` so that wire decoding
    shares the same validation as the in-process
    ``to_dict``/``from_dict`` roundtrip. ``ValueError`` is
    raised for a malformed ``event_class``; the EventLog
    callers handle it via the standard error mapping.

    ADR-016 PR 3: when ``signature`` is present in the wire,
    the JSON dict is passed through to ``Event.from_dict``;
    when absent or empty, ``signature=None`` is passed.
    """

    def s(key: bytes, default: str = "") -> str:
        v = mdata.get(key, default)
        decoded = decode_value(v)
        return decoded if decoded is not None else ""

    # The Redis stream entries encode the correlation metadata
    # as a JSON string and the event data as a JSON string.
    # Decode them here so the in-process to_dict/from_dict
    # contract (dicts, not strings) holds.
    correlation_dict = {
        "correlation_id": s(b"correlation_id"),
        "causation_id": s(b"causation_id"),
        "span_id": s(b"span_id"),
        "metadata": json.loads(s(b"metadata", "{}")),
    }
    sig_raw = s(b"signature", "")
    sig_obj: Optional[dict[str, Any]] = None
    if sig_raw:
        try:
            sig_obj = json.loads(sig_raw)
        except json.JSONDecodeError:
            # Corrupted signature on the wire: treat as absent
            # so downstream verify_event sees signature=None
            # and returns False (defensive default).
            sig_obj = None
    return Event.from_dict(
        {
            "event_id": s(b"event_id"),
            "agent_id": s(b"agent_id"),
            "event_type": s(b"event_type"),
            "event_class": s(b"event_class"),
            "timestamp": s(b"timestamp"),
            "data": json.loads(s(b"data", "{}")),
            "correlation": correlation_dict,
            "causation_id": s(b"causation_id"),
            "version": s(b"version", "1"),
            "signature": sig_obj,
        }
    )


__all__ = ["event_to_redis", "parse_event"]
