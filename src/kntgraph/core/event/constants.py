# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event.constants -- Module-level constants for the event model.

Two responsibilities:

  - The `EventClass` Literal + runtime-validated
    `ALLOWED_EVENT_CLASSES` set. The Literal is the
    static-only promise; the frozenset is what
    `Event.__post_init__` consults to reject
    malformed wire data.

  - The UUID namespaces reserved for deterministic
    event ids. The framework itself does NOT assign
    agent_ids (the application owns that strategy);
    these namespaces are kept for future
    framework-owned deterministic ids.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from ..agent_id import AGENT_ID_RE


# Deterministic namespaces for uuid5. Reserved for future
# framework-owned deterministic ids; the framework itself does
# not currently assign agent_ids — the application owns that.
FMH_EVENT_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
FMH_AGENT_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

# Two event classes share the same stream; the value flows in
# `event.event_class` so systems can filter.
EventClass = Literal["lifecycle", "domain"]

# Runtime-validated set of event_class values. MUST stay in
# sync with the `EventClass` Literal above. ``Event.__post_init__``
# consults this frozenset; the Literal hint keeps mypy honest.
ALLOWED_EVENT_CLASSES: frozenset[str] = frozenset({"lifecycle", "domain"})

# Backwards-compat re-export of ``AGENT_ID_RE`` under the
# historical private name. New code should import from
# ``kntgraph.core.agent_id`` directly.
_AGENT_ID_RE = AGENT_ID_RE


__all__ = [
    "ALLOWED_EVENT_CLASSES",
    "EventClass",
    "FMH_AGENT_NAMESPACE",
    "FMH_EVENT_NAMESPACE",
]
