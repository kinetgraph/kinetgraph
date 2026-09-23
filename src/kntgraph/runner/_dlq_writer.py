# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
runner._dlq_writer -- bridge ``*.dlq`` events to the DLQ.

The saga emits typed ``*.dlq`` events (e.g.
``saga.<saga_name>.dlq``) to keep itself side-effect-free
(ADR-069 §5.2). This module is the **adapter** the design
calls out: it reads the ``*.dlq`` events from the outgoing
batch and pushes a :class:`DeadLetterEvent` to the
:class:`DeadLetterQueue`.

The saga's
:func:`concordos.saga._records.dlq_event` docstring
captures the contract:

    The actual DLQ insertion is performed by an adapter
    system that reads this event and appends to
    ``knt:dlq:saga:<name>``; the saga system only emits
    the typed event so the DLQ adapter stays out of the
    saga's dependency graph.

This module is that adapter. It is called from
:func:`kntgraph.runner._systems_runner.append_system_outgoing`
right after the EventLog append -- the same hook point the
compensation counter uses, so the durability ordering
(EventLog commit before DLQ write) is preserved.

Idempotency
-----------

``DeadLetterQueue.append()`` is already idempotent on
``<event_id>:<reason>`` (see
:mod:`kntgraph.infra.redis._dlq`). The writer inherits
that property: re-running on the same ``*.dlq`` event --
e.g. after a dispatcher restart that replays the same
batch -- is a no-op. The storage returns the sentinel
``PLACEHOLDER`` for the dedup hit, which the writer
filters out of the returned ``stream_ids`` list.

Hook point
----------

The writer is invoked from
:func:`kntgraph.runner._systems_runner.append_system_outgoing`
once per dispatch tick, after the outgoing batch has
been appended to the EventLog. It iterates ``outgoing``
in order, calls :meth:`DeadLetterQueue.append` for every
event whose ``event_type`` ends with
:data:`DLQ_EVENT_SUFFIX`, and returns the list of stream
ids assigned by the storage (one per fresh entry).

Failure handling
----------------

A storage error on one event (the
``Result.err_value()`` arm) is logged via ``structlog``
and the loop continues with the next event. A failure on
event ``N`` does NOT block events ``N+1...`` -- the DLQ
is best-effort and the EventLog is the source of truth.
The saga stays side-effect-free even when the adapter is
down: the ``*.dlq`` event is in the log and a future
replay will retry the append.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog

from ..core.event import Event
from ..events.dlq.store import DeadLetterQueue
from ..events.dlq.values import DLQReason, DeadLetterEvent


logger = structlog.get_logger()


#: Event-type suffix that marks an event as DLQ-bound. The
#: saga emits ``saga.<name>.dlq``; future event types
#: sharing the same shape (e.g. nested-saga compensation
#: failure) just extend the suffix.
DLQ_EVENT_SUFFIX = ".dlq"

#: Sentinel returned by ``DeadLetterQueue.append`` when
#: the storage layer detected a duplicate ``<event_id>:<reason>``
#: pair (idempotency hit). Re-exposed here so the test
#: suite can pin the contract without reaching into
#: ``infra.redis._dlq``.
PLACEHOLDER = "PLACEHOLDER"

#: Default :class:`DLQReason` when the ``*.dlq`` event has
#: no explicit ``reason`` in its data payload. The saga's
#: ``saga.<name>.dlq`` event signals a compensation failure
#: -- ``PROCESSING_FAILED`` is the most general-purpose
#: bucket. ADR-075 §3.1 keeps the dedicated
#: ``TOOL_STALE_*`` reasons for the TTL sweeper, so those
#: paths do not collide with this default.
DEFAULT_DLQ_REASON = DLQReason.PROCESSING_FAILED


async def append_dlq_events(
    outgoing: list[Event],
    dlq: Optional[DeadLetterQueue],
    *,
    default_reason: DLQReason = DEFAULT_DLQ_REASON,
) -> list[str]:
    """Push every ``*.dlq`` event in ``outgoing`` to the DLQ.

    Iterates ``outgoing`` in order. For each event whose
    ``event_type`` ends with :data:`DLQ_EVENT_SUFFIX`, builds
    a :class:`DeadLetterEvent` and calls
    :meth:`DeadLetterQueue.append`. Storage errors are
    logged but do NOT abort the loop -- one failed event
    does not block the others.

    Args:
        outgoing: the events the dispatcher's tick loop
            just appended to the EventLog. Same list the
            compensation counter scans.
        dlq: the :class:`DeadLetterQueue` instance the
            dispatcher was constructed with (``None``
            when the application did not opt in to a
            DLQ). When ``None`` the function is a no-op
            and returns ``[]`` -- the saga stays
            side-effect-free.
        default_reason: :class:`DLQReason` assigned when
            the event's ``data`` payload has no ``reason``
            key (saga-emitted events fall in this bucket).
            Operators may pass a different default for
            verticals with a richer failure vocabulary.

    Returns:
        List of stream ids assigned by the storage, one
        per fresh entry. Idempotent re-runs (dedup hit)
        are filtered out -- the caller does not need to
        distinguish "wrote" from "already there" for the
        metrics path.
    """
    if dlq is None or not outgoing:
        return []

    stream_ids: list[str] = []
    now = datetime.now(tz=timezone.utc)
    for event in outgoing:
        if not event.event_type.endswith(DLQ_EVENT_SUFFIX):
            continue

        # ``Event.data`` is the JSON-serialisable union
        # (``JsonValue``) defined in ``core._typing``.
        # ``Mapping`` covers the common case; ``dict`` is
        # the runtime type when the event was built with
        # a literal dict. Other types (``str``, ``int``,
        # ``list``) skip the ``reason`` / ``error`` lookups
        # and fall back to the defaults below.
        data: dict = dict(event.data) if isinstance(event.data, dict) else {}
        reason_str = data.get("reason")
        try:
            reason = DLQReason(reason_str) if reason_str else default_reason
        except ValueError:
            # An unrecognised ``reason`` value is not a
            # bug in the writer -- the event's emitter
            # may be a forward-compat vertical. Fall back
            # to the default rather than crash the loop.
            reason = default_reason
        error_message = str(data.get("error", "compensation failed"))

        dl_event = DeadLetterEvent(
            event=event,
            reason=reason,
            error_message=error_message,
            original_timestamp=event.timestamp,
            dlq_timestamp=now,
            retry_count=0,
            metadata=data,
        )

        result = await dlq.append(dl_event)
        if result.is_err():
            # Storage failure. Log and move on -- the
            # ``*.dlq`` event is durably committed to the
            # EventLog; a future replay will retry the
            # append (the storage's idempotency boundary
            # closes the duplicate window).
            logger.warning(
                "dlq_writer.append_failed",
                event_id=str(event.event_id),
                event_type=event.event_type,
                agent_id=event.agent_id,
                error=str(result.err_value()),
            )
            continue
        stream_id = result.ok_value()
        # ``ok_value`` returns ``T | None`` at the type
        # level even after the ``is_err()`` check; narrow
        # with an isinstance so pyright sees ``str`` (the
        # ``is_err()`` branch above guarantees the value
        # is present).
        if not isinstance(stream_id, str):
            continue
        if stream_id == PLACEHOLDER:
            # Idempotency hit; the event is already in
            # the DLQ. Not a fresh write, so we do not
            # surface the placeholder in the return
            # list (the caller does not care about
            # "wrote vs already there" for accounting).
            logger.debug(
                "dlq_writer.idempotent_skip",
                event_id=str(event.event_id),
                event_type=event.event_type,
            )
            continue
        stream_ids.append(stream_id)
    return stream_ids


__all__ = [
    "DLQ_EVENT_SUFFIX",
    "DEFAULT_DLQ_REASON",
    "append_dlq_events",
]
