# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
continuity.recorders.tool -- Build `continuity.tool_used` events.

Pure function: takes the raw inputs the manager
collected, returns a fully-shaped `Event` wrapped in
``Result`` (the framework's Railway type). The PII
rule (ADR-014 §2.4) is the caller's responsibility:
``params_fingerprint`` and ``result_signature`` MUST
be hashes, not the raw values. This module does not
re-validate the hash shape — only the manager-side
`record_tool_used` enforces the "non-empty tool
name" rule (which is a different concern from the
PII gate).

The signature returns ``Result`` for symmetry with
``build_entity_seen_event`` (so the manager can use
the same ``.bind`` pattern on every record path) and
to make the future addition of new validation rules
non-breaking for the call sites.
"""

from __future__ import annotations

from ....core.event import CorrelationContext, Event
from ....core.result import Ok, PersistenceError, Result
from ..state import ContinuityEventType


def build_tool_used_event(
    *,
    agent_id: str,
    correlation: CorrelationContext,
    tool: str,
    params_fingerprint: str,
    result_signature: str,
    latency_ms: int,
) -> Result[Event, PersistenceError]:
    """
    Build a `continuity.tool_used` event.

    `params_fingerprint` MUST be a hash of the params,
    not the params themselves (ADR-014 §2.4).
    `result_signature` MUST be a hash of the result,
    not the raw result. The caller is responsible for
    enforcing both rules.

    Always returns ``Ok(Event)`` for valid input
    (the PII-hash shape is the caller's contract);
    the ``Result`` signature is kept for symmetry
    with the other recorders.
    """
    return Ok(
        Event.domain_from(
            agent_id=agent_id,
            type=ContinuityEventType.TOOL_USED,
            data={
                "tool": tool,
                "params_fingerprint": params_fingerprint,
                "result_signature": result_signature,
                "latency_ms": int(latency_ms),
            },
            correlation=correlation,
        )
    )


__all__ = ["build_tool_used_event"]
