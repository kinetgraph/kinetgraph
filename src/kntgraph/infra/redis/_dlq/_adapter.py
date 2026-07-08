# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
DLQStorage — domain Protocol for the Dead Letter Queue.

Iteration 5 (ADR-019). The DLQ has 4 Redis keys and a
hash-based idempotency protocol (parallel to the SET-based
EventLog). The Protocol abstracts all of that; the
``DeadLetterQueue`` class becomes a thin facade that builds
``DeadLetterEvent`` from the parsed dicts.

Result contract (AGENTS.md §6): all operations return
``Result[T, MemoryError]``. See module docstring.
"""

from __future__ import annotations

from typing import Mapping, Optional, Protocol, runtime_checkable

from kntgraph.core.result import Result

from .._errors import MemoryError


@runtime_checkable
class DLQStorage(Protocol):
    """Domain interface for the DLQ storage layer."""

    async def append(
        self,
        idem_key: str,
        payload: Mapping[str, str],
    ) -> Result[str, MemoryError]:
        """Append a DLQ entry idempotently on ``idem_key``.

        Returns ``Ok(stream_id)`` on success (or replay);
        ``Err(MemoryError)`` on Redis failure. The
        ``idem_key`` is the dedup boundary
        (e.g. ``<event_id>:<reason>``).
        """
        ...

    async def read(
        self, stream_id: str
    ) -> Result[Optional[Mapping[str, str]], MemoryError]:
        """Read a single DLQ entry by stream id.

        Returns ``Ok(dict)`` on hit, ``Ok(None)`` on miss,
        ``Err(MemoryError)`` on Redis failure.
        """
        ...

    async def list_for_agent(
        self, agent_id: str, count: int = 100
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """List DLQ entries for one agent (forward-scan from head)."""
        ...

    async def list_by_reason(
        self, reason: str, count: int = 100
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """List DLQ entries with a given reason (full scan)."""
        ...

    async def list_all(
        self, count: int = 100
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """List DLQ entries (full scan)."""
        ...

    async def read_index(
        self, event_id: str, reason: str
    ) -> Result[Optional[str], MemoryError]:
        """Look up the stream id for ``(event_id, reason)``.

        Returns ``Ok(stream_id)`` on hit, ``Ok(None)`` on
        miss, ``Err(MemoryError)`` on Redis failure.
        """
        ...

    async def find_by_event_id(
        self, event_id: str
    ) -> Result[Optional[str], MemoryError]:
        """Find the first stream id for an ``event_id`` across all reasons.

        Scans the ``<event_id>:*`` keys of the per-event_id
        index. Used by ``DeadLetterQueue.get_event`` to look
        up an entry without knowing the reason.

        Returns ``Ok(stream_id)`` on hit, ``Ok(None)`` on
        miss, ``Err(MemoryError)`` on Redis failure.
        """
        ...

    async def bump_reason_counter(
        self, reason: str, delta: int
    ) -> Result[None, MemoryError]:
        """HINCRBY the per-reason counter by ``delta``.

        ``delta`` may be negative (decrement on drop).
        """
        ...

    async def get_stats(self) -> Result[dict, MemoryError]:
        """Aggregate stats: total events, unique agents, by-reason.

        Returns ``Ok(dict)`` with keys ``total_events``,
        ``unique_agents``, ``by_reason``.
        """
        ...

    async def purge(self) -> Result[int, MemoryError]:
        """Wipe all 4 DLQ keys. Returns the number of entries purged."""
        ...

    async def drop_entry(
        self,
        event_id: str,
        reason: str,
        stream_id: str,
    ) -> Result[None, MemoryError]:
        """Remove a single DLQ entry: XDEL + HDEL on the per-event_id index.

        The ``stream_id`` is passed in (looked up by the
        caller via ``read_index``) to avoid a second HGET.
        Skips XDEL when ``stream_id == "PLACEHOLDER"`` (the
        in-flight marker from a concurrent claim).
        """
        ...


__all__ = ["DLQStorage"]
