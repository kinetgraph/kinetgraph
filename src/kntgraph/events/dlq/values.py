# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
dlq.values -- Data types and constants for the Dead Letter Queue.

Three layers, kept on this module because each is small
and they are consumed together by ``events/dlq/store.py``:

  - `DLQReason` (enum): the closed set of failure modes
    that can land an event in the DLQ.

  - `DeadLetterEvent` (frozen dataclass): the cached
    payload of a DLQ entry. Carries the original `Event`
    plus failure metadata (reason, error_message,
    retry_count, original_timestamp, dlq_timestamp,
    metadata).

  - The four Redis key **suffix templates** (stream + 3
    indexes). These are bare ``"knt:dlq:..."`` strings;
    the storage adapter composes them with the namespace
    prefix at every read/write (ADR-076). Single source of
    truth lives in :mod:`kntgraph.infra.redis._dlq`; this
    module re-exports them so legacy callers (and the
    ``DeadLetterQueue`` orchestrator) do not need to import
    across the framework boundary.

The codec (`to_dict` / `from_dict`) lives on
`DeadLetterEvent` because the shape is intrinsically
tied to the data class — splitting it into a separate
`codec.py` module buys nothing here.

No I/O, no Redis, no event construction.
"""

from __future__ import annotations

# ADR-076: Redis key suffix templates are the single source
# of truth in ``infra.redis._dlq``. Re-export them here so
# the vertical (``events/dlq``) does not duplicate the
# wire format -- the prefix composition lives in the
# adapter, the suffixes here are pure strings.
from ...infra.redis._dlq import (
    DLQ_AGENT_INDEX,
    DLQ_EVENT_INDEX,
    DLQ_REASON_INDEX,
    DLQ_STREAM_KEY,
)

# ``DLQReason`` and ``DeadLetterEvent`` live in the
# framework (``runner._dlq_protocol`` -- the Protocol's
# home) so the runner can import the types without
# crossing the framework→vertical boundary. The vertical
# re-exports them so existing callers do not need to
# update their imports. This is the same pattern as the
# ``CachedSolution`` move (see
# ``core/components/solution.py``).
from ...runner._dlq_protocol import (
    DeadLetterEvent,
    DLQReason,
)

__all__ = [
    "DLQ_AGENT_INDEX",
    "DLQ_EVENT_INDEX",
    "DLQ_REASON_INDEX",
    "DLQ_STREAM_KEY",
    "DLQReason",
    "DeadLetterEvent",
]

# The ``to_dict`` / ``from_dict`` codec for ``DeadLetterEvent``
# lives in ``events/dlq/store.py`` (the wire format is owned
# by the DLQ facade, not by the framework's Protocol
# module). The codec depends on ``Event.from_dict`` which
# lives in core -- splitting the codec from the dataclass
# avoids an import cycle.


__all__ = [
    "DLQ_AGENT_INDEX",
    "DLQ_EVENT_INDEX",
    "DLQ_REASON_INDEX",
    "DLQ_STREAM_KEY",
    "DLQReason",
    "DeadLetterEvent",
]
