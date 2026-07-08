# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
continuity.state -- Pure data types and constants for continuity.

Two layers:

  - Module-level constants: `CONTINUITY_KEY_PREFIX`
    (the Redis key namespace), `DEFAULT_TTL_SECONDS`
    (the sliding TTL used when the caller doesn't
    supply one), `MAX_FIELD_VALUE_LEN` (defensive
    bound on the size of a value stored in a Redis
    Hash field).

  - `ContinuityEventType` (string constants for the
    continuity vocabulary per ADR-014).

  - `ContinuityState` (frozen dataclass): the cached
    projection of the recent state-of-use of a
    (tenant, user) pair. All "slot" fields are dicts
    mapping the natural key of the slot to its latest
    value; the hash layout of the cache mirrors these
    dicts with prefixed keys.

No I/O, no Redis, no event construction. The
recorders in `continuity.recorders` and the cache
codec in `continuity.cache_codec` are the only
modules that touch Redis or build events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


CONTINUITY_KEY_PREFIX = "fmh:continuity:"

# Backwards-compat re-export. The default TTL now lives
# in ``Settings.continuity_ttl_seconds`` (90 days,
# sliding); this constant is kept for downstream code
# that imported it.
DEFAULT_TTL_SECONDS: Optional[int] = 90 * 24 * 3600

# Maximum length of the stored value component (result_signature,
# tool name, etc.) before truncation in the cache. Defensive
# bound to keep the Redis Hash field size predictable.
MAX_FIELD_VALUE_LEN = 256


class ContinuityEventType:
    CREATED = "continuity.created"
    TOOL_USED = "continuity.tool_used"
    ENTITY_SEEN = "continuity.entity_seen"
    CATEGORY_CHOSEN = "continuity.category_chosen"
    CLEARED = "continuity.cleared"


@dataclass(frozen=True, slots=True)
class ContinuityState:
    """
    Cached projection of the recent state-of-use of a (tenant,
    user) pair.

    All fields are dicts that map the natural key of the slot
    to its latest value. The hash layout of the cache mirrors
    these dicts with prefixed keys.
    """

    tenant_id: str
    user_id: str
    last_tools: dict[str, str] = field(default_factory=dict)
    last_entities: dict[str, str] = field(default_factory=dict)
    last_categories: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    cleared_at: Optional[float] = None

    def is_cleared(self) -> bool:
        return self.cleared_at is not None


__all__ = [
    "CONTINUITY_KEY_PREFIX",
    "ContinuityEventType",
    "ContinuityState",
    "DEFAULT_TTL_SECONDS",
    "MAX_FIELD_VALUE_LEN",
]
