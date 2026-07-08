# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event.codec -- Wire format for `Event`.

`to_dict` and `from_dict` are the canonical Event ↔
dict transformations. They are additive (a missing
`signature` field round-trips to `signature=None`).

`to_json` and `from_json` are thin wrappers over the
dict variants that go through `json.dumps` /
`json.loads`. They are convenient for tests and for
ad-hoc serialisation; the Redis wire path uses
`to_dict` directly.

`from_dict` delegates to `Event.create` so all
construction paths share the same validation and
the same deterministic-id generation (the latter
is bypassed when an explicit `event_id` is passed).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID

if TYPE_CHECKING:
    from kntgraph.core.event.event import Event
    from kntgraph.security.signing import Signature


def event_to_dict(event: Event) -> dict:
    d = {
        "event_id": str(event.event_id),
        "agent_id": event.agent_id,
        "event_type": event.event_type,
        "event_class": event.event_class,
        "timestamp": event.timestamp.isoformat(),
        "data": dict(event.data),
        "correlation": event.correlation.to_dict(),
        "causation_id": str(event.causation_id) if event.causation_id else "",
        "version": event.version,
    }
    # ``signature`` is additive (ADR-016 PR 2). Omitted
    # from the wire when absent; old consumers ignore
    # unknown keys via the existing ``from_dict`` extra-
    # ignore behaviour.
    if event.signature is not None:
        d["signature"] = event.signature.to_dict()
    return d


def event_from_dict(d: dict) -> Event:
    """
    Inverse of ``event_to_dict``. Delegates to ``Event.create``
    so that all construction paths share the same
    validation and the same deterministic-id generation
    (the latter is bypassed when an explicit ``event_id``
    is passed).

    Raises ``ValueError`` for malformed ``event_class``
    via ``Event.__post_init__``.
    """
    # The wire format encodes `version` as a string (Redis
    # values are always bytes/str). Normalize to int.
    raw_version = d.get("version") or 1
    version = int(raw_version)
    # ``signature`` is optional (ADR-016 PR 2). When
    # present, decode via ``Signature.from_dict``. The
    # import is local to avoid loading ``security``
    # when the caller only does JSON round-tripping
    # without a signature (the common path).
    sig_obj: Optional["Signature"] = None  # noqa: F821
    if d.get("signature"):
        from kntgraph.security.signing import Signature

        sig_obj = Signature.from_dict(d["signature"])
    # Local imports to avoid the cycle
    # `event` ↔ `correlation` at module load time.
    from .correlation import CorrelationContext
    from .event import Event

    return Event.create(
        event_type=d["event_type"],
        agent_id=d["agent_id"],
        event_class=d["event_class"],
        data=d.get("data") or {},
        correlation=CorrelationContext.from_dict(d.get("correlation") or {}),
        causation_id=(UUID(d["causation_id"]) if d.get("causation_id") else None),
        event_id=UUID(d["event_id"]),
        timestamp=datetime.fromisoformat(d["timestamp"]),
        version=version,
        signature=sig_obj,
    )


def event_to_json(event: Event) -> str:
    return json.dumps(event_to_dict(event), default=str, sort_keys=True)


def event_from_json(s: str) -> Event:
    return event_from_dict(json.loads(s))


__all__ = [
    "event_from_dict",
    "event_from_json",
    "event_to_dict",
    "event_to_json",
]
