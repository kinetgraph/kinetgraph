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
from collections.abc import Mapping
from uuid import UUID, uuid5

from .._typing import JsonValue
from .constants import KNT_EVENT_NAMESPACE


def generate_deterministic_event_id(
    causation_id: UUID | str,
    event_type: str,
    data: Mapping[str, JsonValue],
    *,
    agent_id: str | None = None,
    idempotency_key: str | None = None,
) -> UUID:
    """
    Idempotent event id.

    The hash has two modes:

    **Default mode** (no ``idempotency_key``):
    The hash is over:
      - ``causation_id`` (or a stable placeholder if None)
      - ``event_type``
      - ``data`` (JSON-serialised, sorted keys)
      - ``agent_id`` (only if explicitly passed; the existing
        signature does not include it, but ``Event.create``
        passes it for additional safety against collisions
        between agents).

    This is the legacy behaviour, preserved verbatim. The
    ``causation_id`` enters the hash as a defensive
    collision protection: events that causally depend on
    different parents get different ids.

    **Idempotency-anchored mode** (``idempotency_key`` is set):
    The hash is over:
      - ``idempotency_key`` (the canonical request identity)
      - ``agent_id``
      - ``event_type``
      - ``data``

    ``causation_id`` is **deliberately excluded** — the
    idempotency_key IS the logical request identity, so
    two re-emits that causally depend on different parents
    (e.g. a tool failure → retry) collapse to the same
    UUID when they target the same logical request. The
    EventLog idempotency index dedupes the second append,
    stopping the retry storm that the backoffice
    2026-10-07 post-mortem documented on
    ``document_parser`` (six retries of the same logical
    request landed six separate events because each
    attempt's ``causation_id`` differed).

    The ``Event`` itself still carries the
    ``causation_id`` field, so the audit chain is intact
    (only the ``event_id`` derivation is anchored on the
    idempotency_key).

    Same inputs → same UUID. Allows safe replay without
    duplicating events in the stream.
    """
    payload_str = json.dumps(dict(data), sort_keys=True, default=str)
    if idempotency_key is not None:
        # Idempotency-anchored mode: ``causation_id`` is
        # not part of the hash. The caller is asserting
        # that the ``idempotency_key`` IS the canonical
        # identity of the logical request.
        base = f"ik:{idempotency_key}|{agent_id or ''}|{event_type}|{payload_str}"
    else:
        # Default mode: causation_id is in the hash
        # (legacy behaviour; defensive collision
        # protection).
        base = f"{causation_id}|{event_type}|{payload_str}"
        if agent_id is not None:
            base = f"{agent_id}|{base}"
    return uuid5(KNT_EVENT_NAMESPACE, base)


__all__ = ["generate_deterministic_event_id"]
