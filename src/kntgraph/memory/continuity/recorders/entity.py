# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
continuity.recorders.entity -- Build `continuity.entity_seen` events.

Pure function: takes the raw inputs the manager
collected, returns a fully-shaped `Event` wrapped in
``Result`` (the framework's Railway type). The PII
gate (ADR-014 §2.7) is enforced **here** via
``pii.check_pii_hash``, not on the manager: the
recorder is the single place that constructs a
``continuity.entity_seen`` event, so the gate is
colocated with the event shape.

A future ``entity_seen`` builder (e.g. with extra
metadata) inherits the same gate by default.
"""

from __future__ import annotations

from ....core.event import CorrelationContext, Event
from ....core.result import Err, Ok, PersistenceError, Result
from ..pii import check_pii_hash
from ..state import ContinuityEventType


def build_entity_seen_event(
    *,
    agent_id: str,
    correlation: CorrelationContext,
    kind: str,
    value_hash: str,
    source: str,
) -> Result[Event, PersistenceError]:
    """
    Build a `continuity.entity_seen` event.

    `value_hash` MUST already be a hash — the PII gate
    returns ``Err(PersistenceError(...))`` if it isn't
    (the manager propagates the ``Err`` to the caller).
    The raw value is never accepted here.

    Returns ``Ok(Event)`` on success or
    ``Err(PersistenceError(...))`` when the PII gate
    rejects the input. The Railway signature lets the
    manager compose the result with `.bind` against
    `_emit_and_refresh`, so there is no `try/except`
    on the hot path.
    """
    pii_check = check_pii_hash(value_hash)
    if pii_check.is_err():
        return Err(pii_check.err_value())  # type: ignore[arg-type]
    return Ok(
        Event.domain_from(
            agent_id=agent_id,
            type=ContinuityEventType.ENTITY_SEEN,
            data={
                "kind": kind,
                "value_hash": value_hash,
                "source": source,
            },
            correlation=correlation,
        )
    )


__all__ = ["build_entity_seen_event"]
