# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
FMH Events — dead-letter queue for events that failed processing.

The main event log lives in `kntgraph.stream.EventLog`. This
package provides the failure-recovery side: when a system cannot
process an event (after retries), the event is parked in the DLQ
with a reason. The DLQ supports reprocessing (re-emit to the
EventLog) and discarding (poison pills).
"""

from .dlq import (
    DLQ_AGENT_INDEX,
    DLQ_EVENT_INDEX,
    DLQ_REASON_INDEX,
    DLQ_STREAM_KEY,
    DLQReason,
    DeadLetterActions,
    DeadLetterEvent,
    DeadLetterQueue,
)

__all__ = [
    "DLQ_AGENT_INDEX",
    "DLQ_EVENT_INDEX",
    "DLQ_REASON_INDEX",
    "DLQ_STREAM_KEY",
    "DLQReason",
    "DeadLetterActions",
    "DeadLetterEvent",
    "DeadLetterQueue",
]
