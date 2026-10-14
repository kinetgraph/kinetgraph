# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event.id_helpers -- Deterministic event id generation.

Same inputs → same UUID → the EventLog dedupes on
``event_id`` so a retry that arrives after the first
request is appended simply no-ops.

Two modes: default (hash includes ``causation_id``) and
idempotency-anchored (hash excludes ``causation_id``;
the idempotency_key becomes the canonical request
identity — the backoffice 2026-10-07 retry-storm fix
on ``document_parser``).
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
    """Idempotent event id (CC ≤ 2). See module docstring
    for the two-mode contract.
    """
    if idempotency_key is not None:
        return _idempotent_event_id(idempotency_key, agent_id, event_type, data)
    return _legacy_event_id(causation_id, agent_id, event_type, data)


def _idempotent_event_id(
    idempotency_key: str,
    agent_id: str | None,
    event_type: str,
    data: Mapping[str, JsonValue],
) -> UUID:
    """Idempotency-anchored hash — ``causation_id`` is
    excluded so re-emits of the same logical request
    collapse to the same UUID (backoffice 2026-10-07
    retry-storm fix on ``document_parser``).
    """
    payload_str = json.dumps(dict(data), sort_keys=True, default=str)
    base = f"ik:{idempotency_key}|{agent_id or ''}|{event_type}|{payload_str}"
    return uuid5(KNT_EVENT_NAMESPACE, base)


def _legacy_event_id(
    causation_id: UUID | str,
    agent_id: str | None,
    event_type: str,
    data: Mapping[str, JsonValue],
) -> UUID:
    """Legacy / default hash — ``causation_id`` enters
    the hash as a defensive collision protection.
    """
    payload_str = json.dumps(dict(data), sort_keys=True, default=str)
    base = f"{causation_id}|{event_type}|{payload_str}"
    if agent_id is not None:
        base = f"{agent_id}|{base}"
    return uuid5(KNT_EVENT_NAMESPACE, base)


__all__ = ["generate_deterministic_event_id"]
