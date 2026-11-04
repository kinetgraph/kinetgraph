# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Slot-grouped blocking read for the EventLog fan-in.

A standalone server accepts one ``XREAD`` over any number of
streams. A cluster server accepts it only over streams that
share a hash slot, and the framework's per-agent streams do
not share one -- so the fan-in is split per slot.

The split changes what the subscriber holds: a standalone
subscriber holds one connection for the whole wait, a cluster
subscriber holds one connection per read and reads the groups
one at a time. ADR-068 §3.2 made the single-connection fan-in
a hard requirement (a fleet of 100 agents behind a 30-connection
pool exhausts it on day one), so the cluster behaviour is a
documented degradation, not an equivalent: see DEBT §2.41.

The cursor bookkeeping is unaffected. Each group is read from
the caller's cursor, the groups that were not read keep their
cursor, and the next call resumes where it stopped -- no event
is skipped.
"""

from __future__ import annotations

from kntgraph.core.event import Event

from .._client import RedisLike
from .._cluster import group_keys_by_slot


async def read_streams_by_slot(
    client: RedisLike,
    streams: dict[str, str],
    *,
    block_ms: int,
    count: int | None,
    prefix: str,
) -> tuple[dict[str, str], list[Event]]:
    """Read the streams, split per hash slot when the client is a cluster driver.

    Returns ``(new_cursors, events)``: the per-stream id to
    persist as the next cursor, and the events in arrival
    order. A timeout with no arrivals yields ``({}, [])``.

    The block budget is divided between the groups, so a whole
    cycle waits at most ``block_ms``. The cycle ends at the
    first group with an arrival: the caller dispatches the
    agents that woke and reads the remaining groups on the next
    call.
    """
    # The decoder lives in the adapter module, which imports
    # this one; the import is deferred to the call to break the
    # cycle (the same shape as the codec import in the adapter).
    from ._adapter import _flatten_xread_response

    groups = group_keys_by_slot(client, list(streams))
    if len(groups) <= 1:
        response = await client.xread(streams=streams, count=count, block=block_ms)
        return _flatten_xread_response(response, prefix)

    per_group_ms = block_ms // len(groups)
    new_cursors: dict[str, str] = {}
    events: list[Event] = []
    for group in groups:
        group_streams = {key: streams[key] for key in group}
        response = await client.xread(
            streams=group_streams, count=count, block=per_group_ms
        )
        group_cursors, group_events = _flatten_xread_response(response, prefix)
        new_cursors.update(group_cursors)
        events.extend(group_events)
        if new_cursors:
            break
    return new_cursors, events


__all__ = ["read_streams_by_slot"]
