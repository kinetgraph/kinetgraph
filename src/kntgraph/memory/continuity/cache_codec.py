# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
continuity.cache_codec -- Hash layout for the continuity cache.

Two pure helpers, both stateless:

  - `serialize_for_cache(state)`: encode a
    `ContinuityState` to the dict-of-strings that
    `HSET` expects. Prefixes are `tool:`, `entity:`,
    `last:` for the slot dicts, and bare keys
    (`tenant_id`, `user_id`, `created_at`,
    `updated_at`, `cleared_at`) for the scalars.
    Values are truncated to `MAX_FIELD_VALUE_LEN` to
    keep the Redis Hash field size predictable.

  - `read_cache(raw)`: decode a `HGETALL` result (a
    flat dict of strings) into a `ContinuityState`.
    Returns `None` when the hash is empty or has no
    `created_at` (a continuity that has not been
    `created` yet).

No Redis client is held by this module — the
manager passes the raw `HGETALL` result. Keeping the
codec pure means tests can exercise it with literal
dicts (no fakeredis needed).
"""

from __future__ import annotations

from typing import Optional

from ...infra.redis._codec import decode_dict
from .state import ContinuityState, MAX_FIELD_VALUE_LEN


def serialize_for_cache(
    state: ContinuityState,
) -> dict[str, str]:
    """
    Encode a `ContinuityState` to a Hash mapping for
    `HSET`. The same dict is consumed by the manager's
    `_store_cache` and the `_write_cache_for_key`
    helper on the base class.

    Scalar fields go in by their bare name; slot
    dicts are prefixed (`tool:`, `entity:`, `last:`).
    Values are truncated to `MAX_FIELD_VALUE_LEN` to
    keep the Redis Hash field size predictable.
    """
    mapping: dict[str, str] = {
        "tenant_id": state.tenant_id,
        "user_id": state.user_id,
        "created_at": str(state.created_at),
        "updated_at": str(state.updated_at),
    }
    if state.cleared_at is not None:
        mapping["cleared_at"] = str(state.cleared_at)
    for tool, sig in state.last_tools.items():
        mapping[f"tool:{tool}"] = sig[:MAX_FIELD_VALUE_LEN]
    for ent, at in state.last_entities.items():
        mapping[f"entity:{ent}"] = at[:MAX_FIELD_VALUE_LEN]
    for slot, value in state.last_categories.items():
        mapping[f"last:{slot}"] = value[:MAX_FIELD_VALUE_LEN]
    return mapping


def read_cache(
    raw: dict[bytes, bytes],
) -> Optional[ContinuityState]:
    """
    Decode a `HGETALL` result into a `ContinuityState`.

    Returns `None` when the hash is empty (cache miss)
    or has no `created_at` field (a continuity that
    has not been `created` yet — we treat it as
    "nothing to read" so callers can early-return).

    The `raw` argument is the bytes-keyed dict that
    comes directly from `redis.asyncio.Redis.hgetall`;
    we run `decode_dict` to convert to strings before
    parsing. The split is here (not in the manager) so
    the codec is testable with plain string dicts.
    """
    if not raw:
        return None
    decoded = decode_dict(raw)

    last_tools: dict[str, str] = {}
    last_entities: dict[str, str] = {}
    last_categories: dict[str, str] = {}

    for k, v in decoded.items():
        if k.startswith("tool:"):
            last_tools[k[len("tool:") :]] = v
        elif k.startswith("entity:"):
            last_entities[k[len("entity:") :]] = v
        elif k.startswith("last:"):
            last_categories[k[len("last:") :]] = v

    # `created_at` is set when `continuity.created` has
    # been folded. If absent, the record has no history.
    if "created_at" not in decoded:
        return None

    cleared_at_raw = decoded.get("cleared_at")
    try:
        cleared_at = float(cleared_at_raw) if cleared_at_raw else None
    except (TypeError, ValueError):
        cleared_at = None

    return ContinuityState(
        tenant_id=decoded.get("tenant_id", ""),
        user_id=decoded.get("user_id", ""),
        last_tools=last_tools,
        last_entities=last_entities,
        last_categories=last_categories,
        created_at=float(decoded.get("created_at", 0.0)),
        updated_at=float(decoded.get("updated_at", 0.0)),
        cleared_at=cleared_at,
    )


__all__ = ["read_cache", "serialize_for_cache"]
