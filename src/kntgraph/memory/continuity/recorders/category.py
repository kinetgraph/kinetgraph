# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
continuity.recorders.category -- Build `continuity.category_chosen` events.

Pure function: takes the raw inputs the manager
collected, returns a fully-shaped `Event` wrapped in
``Result`` (the framework's Railway type). Categorical
slots are CFOP, cost center, etc. — they are not PII
(they are operator-chosen labels, not user data) so no
PII gate applies.

The ``Result`` signature is kept for symmetry with the
other recorders so the manager can use the same
``.bind`` pattern on every record path.
"""

from __future__ import annotations

from ....core.event import CorrelationContext, Event
from ....core.result import Ok, PersistenceError, Result
from ..state import ContinuityEventType


def build_category_chosen_event(
    *,
    agent_id: str,
    correlation: CorrelationContext,
    slot: str,
    value: str,
) -> Result[Event, PersistenceError]:
    """
    Build a `continuity.category_chosen` event (CFOP,
    cost center, etc.).

    The slot and value are operator-chosen labels, not
    user data, so no PII gate applies. Always returns
    ``Ok(Event)`` for valid input; the ``Result``
    signature is kept for symmetry.
    """
    return Ok(
        Event.domain_from(
            agent_id=agent_id,
            type=ContinuityEventType.CATEGORY_CHOSEN,
            data={"slot": slot, "value": value},
            correlation=correlation,
        )
    )


__all__ = ["build_category_chosen_event"]
