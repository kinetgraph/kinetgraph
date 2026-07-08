# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event.id_helpers -- Deterministic event id generation.

`generate_deterministic_event_id` is the foundation of
the framework's HTTP-retry safety: same inputs →
same UUID → the EventLog dedupes on `event_id`, so a
retry that arrives after the first request is appended
simply no-ops.
"""

from __future__ import annotations

import json
from typing import Mapping

from .._typing import JsonValue
from uuid import UUID, uuid5

from .constants import FMH_EVENT_NAMESPACE


def generate_deterministic_event_id(
    causation_id: UUID | str,
    event_type: str,
    data: Mapping[str, JsonValue],
    *,
    agent_id: str | None = None,
) -> UUID:
    """
    Idempotent event id.

    The hash is over:
      - `causation_id` (or a stable placeholder if None)
      - `event_type`
      - `data` (JSON-serialised, sorted keys)
      - `agent_id` (only if explicitly passed; the existing
        signature does not include it, but `Event.create`
        passes it for additional safety against collisions
        between agents).

    Same inputs → same UUID. Allows safe replay without
    duplicating events in the stream.
    """
    payload_str = json.dumps(dict(data), sort_keys=True, default=str)
    base = f"{causation_id}|{event_type}|{payload_str}"
    if agent_id is not None:
        base = f"{agent_id}|{base}"
    return uuid5(FMH_EVENT_NAMESPACE, base)


__all__ = ["generate_deterministic_event_id"]
