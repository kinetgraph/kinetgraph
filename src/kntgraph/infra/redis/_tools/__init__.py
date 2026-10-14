# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Redis tool-queue adapter -- sub-package re-exports.

Public API
----------

- :data:`TOOL_QUEUE_KEY_TEMPLATE` -- the per-tool Stream key
  suffix template (``"knt:tools:{tool_name}:queue"``).
- :func:`tool_queue_key` -- the namespaced key builder used
  by :class:`WorkerManager` and :class:`ToolRouter` (ADR-076).
- :data:`TOOL_EVENT_ID_INDEX` -- the dedup-index key suffix
  template (``"knt:tools:{tool_name}:idem:{event_id}"``,
  ADR-083 §3.1).
- :func:`tool_event_id_key` -- the namespaced dedup-index
  key builder (ADR-083 §3.1).
- :data:`DEDUP_TTL_SECONDS_DEFAULT` -- the default TTL for
  the dedup index (3600s; ADR-083 §3.1).
- :func:`claim_tool_queue_slot` -- the three-phase
  ``XADD + SET NX`` dedup orchestrator used by
  :class:`ToolRouter` when ``dedup=True`` (ADR-083 §3.2).
"""

from ._idempotency import claim_tool_queue_slot
from ._keys import (
    DEDUP_TTL_SECONDS_DEFAULT,
    TOOL_EVENT_ID_INDEX,
    TOOL_QUEUE_KEY_TEMPLATE,
    tool_event_id_key,
    tool_queue_key,
)

__all__ = [
    "DEDUP_TTL_SECONDS_DEFAULT",
    "TOOL_EVENT_ID_INDEX",
    "TOOL_QUEUE_KEY_TEMPLATE",
    "claim_tool_queue_slot",
    "tool_event_id_key",
    "tool_queue_key",
]
