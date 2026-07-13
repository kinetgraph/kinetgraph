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

from collections.abc import Mapping
from typing import Optional, Union

from ...core._typing import JsonValue
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
    raw: Mapping[str, Union[str, JsonValue]],
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> Optional[ContinuityState]:
    """
    Decode a `HGETALL` result into a `ContinuityState`.

    Returns `None` when the hash is empty (cache miss)
    or has no `created_at` field (a continuity that
    has not been `created` yet — we treat it as
    "nothing to read" so callers can early-return).

    The `raw` argument accepts either the bytes-keyed
    dict that comes directly from
    ``redis.asyncio.Redis.hgetall`` (via
    :func:`decode_dict` we normalise the values to
    ``str``) or the already-decoded mapping returned
    by the ``ShortMemoryStorage`` Protocol
    (``Mapping[str, JsonValue]``). Both flavours
    round-trip to the same string-keyed dict, so the
    decode path is shared.

    The ``tenant_id``/``user_id`` kwargs are passed in
    because the Hash layout does NOT embed the
    identity — the manager reads them back from
    ``key_parts`` so the decoded state knows who it
    belongs to.
    """
    if not raw:
        return None
    decoded = _normalise_raw(raw)

    last_tools, last_entities, last_categories = _extract_slots(decoded)
    if "created_at" not in decoded:
        return None

    cleared_at = _coerce_float_or_none(decoded.get("cleared_at"))
    created_at = _coerce_float_or_zero(decoded.get("created_at", 0.0))
    updated_at = _coerce_float_or_zero(decoded.get("updated_at", 0.0))

    return ContinuityState(
        tenant_id=tenant_id or decoded.get("tenant_id", ""),
        user_id=user_id or decoded.get("user_id", ""),
        last_tools=last_tools,
        last_entities=last_entities,
        last_categories=last_categories,
        created_at=created_at,
        updated_at=updated_at,
        cleared_at=cleared_at,
    )


def _normalise_raw(
    raw: Mapping[str, Union[str, JsonValue]],
) -> dict[str, str]:
    """Normalise the HGETALL payload to a flat
    ``dict[str, str]``. Handles both bytes-keyed
    (legacy ``redis.asyncio``) and string-keyed
    (``ShortMemoryStorage``) inputs.
    """
    if any(isinstance(k, bytes) for k in raw):
        bytes_raw: dict[bytes, bytes] = {
            k: v.encode() if isinstance(v, str) else v for k, v in raw.items()
        }
        return decode_dict(bytes_raw)
    return {str(k): str(v) for k, v in raw.items()}


def _extract_slots(
    decoded: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Split the flat hash into the three slot dicts
    (``last_tools``/``last_entities``/``last_categories``)
    keyed by the field prefix. The two scalar fields
    (``created_at``/``updated_at``/``cleared_at``/identity)
    stay in the input dict for the caller.
    """
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
    return last_tools, last_entities, last_categories


def _coerce_float_or_none(value: Union[str, JsonValue, None]) -> Optional[float]:
    """Coerce a ``JsonValue`` (or ``str``) to ``float``;
    returns ``None`` for non-scalar or empty values.
    Used for the optional ``cleared_at`` field.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_float_or_zero(value: Union[str, JsonValue]) -> float:
    """Coerce a ``JsonValue`` to ``float``; returns
    ``0.0`` for non-scalar values. Used for required
    timestamp fields (``created_at``/``updated_at``).
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


__all__ = ["read_cache", "serialize_for_cache"]
