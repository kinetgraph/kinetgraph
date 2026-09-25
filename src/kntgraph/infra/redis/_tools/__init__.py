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
"""

from ._keys import TOOL_QUEUE_KEY_TEMPLATE, tool_queue_key


__all__ = [
    "TOOL_QUEUE_KEY_TEMPLATE",
    "tool_queue_key",
]
