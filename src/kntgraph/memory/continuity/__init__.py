# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Continuity — recent state-of-use per (tenant, user).

A continuity is modeled as an **agent** in the EventLog. Its
agent_id is `"continuity:{tenant_id}:{user_id}"`. Its events
have `event_class="domain"` and follow the continuity
vocabulary (ADR-014):

  - "continuity.created"        : first sighting of the
                                  continuity data: {}
  - "continuity.tool_used"      : a tool call completed
                                  data: {
                                    tool, params_fingerprint,
                                    result_signature,
                                    latency_ms
                                  }
  - "continuity.entity_seen"    : an entity was observed
                                  data: {
                                    kind, value_hash,
                                    source
                                  }
                                  PII rule (ADR-014 §2.7):
                                  ``value_hash`` is sha256
                                  truncated to 16 chars;
                                  ``value`` raw is NEVER
                                  stored in the EventLog of
                                  the continuity agent.
  - "continuity.category_chosen": a categorical slot was picked
                                  data: { slot, value }
  - "continuity.cleared"        : LGPD right-to-erasure (or
                                  retention-expired). Terminal.
                                  data: { reason }
                                  After this, ``read`` returns
                                  an empty state until the
                                  next ``tool_used``.

The state is a small projection: the latest tool used per
tool name, the latest entity per (kind, value_hash), and the
latest categorical slot per slot name. The current state is
derived by fold: each event overwrites the corresponding slot.

The Redis Hash at ``knt:continuity:{tenant_id}:{user_id}`` is
a **cache** with sliding TTL (default 90 days, configurable
via ``ttl_seconds``). On every write, the TTL is renewed
(sliding). On miss, the cache is rebuilt from the EventLog.

Cache layout (prefixed keys, parallel to ``ProfileManager``):

  - ``tool:{tool_name}``     → ``"{result_signature}|{latency_ms}|{at}"``
  - ``entity:{kind}:{value_hash}`` → ``"{at}"``
  - ``last:{slot}``          → ``"{value}|{at}"``
  - ``created_at``, ``updated_at``, ``cleared_at``

Cache format: Redis Hash (``HGETALL``/``HSET`` + ``DEL``). For
a JSON-based cache see ``SessionManager``; for a TTL-less
config-only Hash cache see ``ProfileManager``.

**Separação com ``profile``** (ADR-014 §2.2):
``profile`` modela "o que a PME é" — config estável
(regime tributário, tier SLA, e-mail de NF-e). Este módulo
modela "o que a PME estava fazendo" — última tool, último
cliente, último CFOP. Estado-de-uso recente. TTL sliding.
PII hash-only. LGPD ``cleared``.

This module is a thin facade. The implementation is split
across the `continuity` subpackage:

  - `continuity.state`        — `ContinuityState`,
    `ContinuityEventType`, and the module-level
    constants.
  - `continuity.pii`          — the PII gate
    (`check_pii_hash`, `is_pii_hash`) — single
    source of truth for ADR-014 §2.7. Returns
    ``Result`` so callers compose with `.bind`
    (Railway style).
  - `continuity.recorders`    — pure event builders,
    one per event type (tool/entity/category). The
    entity builder colocates the PII gate.
  - `continuity.fold`         — pure fold
    (`_fold_continuity_events`,
    `_reset_after_clear`).
  - `continuity.cache_codec`  — `read_cache` /
    `serialize_for_cache` (the Hash layout).
  - `continuity.manager`      — `ContinuityManager`
    (the orchestrator; thin glue between the
    pieces above and the `BaseShortTermMemory` base
    class).
"""

from .fold import _fold_continuity_events, _reset_after_clear
from .manager import ContinuityManager
from .pii import PII_HASH_PREFIX, check_pii_hash, is_pii_hash
from .state import (
    CONTINUITY_KEY_PREFIX,
    DEFAULT_TTL_SECONDS,
    MAX_FIELD_VALUE_LEN,
    ContinuityEventType,
    ContinuityState,
)

__all__ = [
    "CONTINUITY_KEY_PREFIX",
    "DEFAULT_TTL_SECONDS",
    "ContinuityEventType",
    "ContinuityManager",
    "ContinuityState",
    "MAX_FIELD_VALUE_LEN",
    "PII_HASH_PREFIX",
    "_fold_continuity_events",
    "_reset_after_clear",
    "check_pii_hash",
    "is_pii_hash",
]
