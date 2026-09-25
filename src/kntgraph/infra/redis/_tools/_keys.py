# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Redis tool-queue keys.

Single source of truth for the wire format of the per-tool
Stream key written by :class:`ToolRouter` and read by
:class:`WorkerManager`:

  - :data:`TOOL_QUEUE_KEY_TEMPLATE` -- suffix template,
    ``"knt:tools:{tool_name}:queue"``.
  - :func:`tool_queue_key` -- compose the prefix with the
    template so the full Redis key is
    ``namespaced(prefix, "knt:tools:{tool_name}:queue")``.

ADR-076 -- namespace prefix
---------------------------

The storage side (EventLog, DLQ, memory tiers,
``WorldCheckpointStorage``) was wired with
``Settings.redis_key_prefix`` in v0.16.0. The dispatcher
side (``WorkerManager``, ``ToolRouter``, the
``stuck_in_queue`` query inside :class:`ReactiveDispatcher`)
was not, leaving two services sharing one Redis still
cross-talking at the tool-queue boundary (DEBT §2.35).

This module is the single point of truth for the
tool-queue wire format. The dispatcher side composes
the prefix at every key build via ``namespaced``,
mirroring the pattern in
:mod:`kntgraph.infra.redis._event_log._keys`.
"""

from __future__ import annotations

from kntgraph.infra.redis._prefix import namespaced


# Suffix template. Pure string; the adapter prepends the
# prefix via :func:`namespaced` at every key build.
TOOL_QUEUE_KEY_TEMPLATE: str = "knt:tools:{tool_name}:queue"
"""Per-tool Redis Stream key (suffix template)."""


def tool_queue_key(prefix: str, tool_name: str) -> str:
    """Build the Stream key for ``tool_name`` under ``prefix``.

    Empty ``prefix`` returns the unprefixed key
    (``"knt:tools:{tool_name}:queue"``) -- byte-for-byte
    identical to the pre-ADR-076 wire format. Non-empty
    ``prefix`` concatenates the prefix in front, producing
    e.g. ``"acme-billing:knt:tools:echo:queue"``.

    The composition is a plain string concatenation -- no
    separator is inserted between the prefix and the
    suffix. Operators who want a trailing colon write
    ``"acme-billing:"`` (note the colon).
    """
    return namespaced(prefix, TOOL_QUEUE_KEY_TEMPLATE.format(tool_name=tool_name))


__all__ = [
    "TOOL_QUEUE_KEY_TEMPLATE",
    "tool_queue_key",
]
