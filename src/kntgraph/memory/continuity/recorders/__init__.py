# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
continuity.recorders -- Pure event builders for the continuity vocabulary.

Each module exposes a single `build_*_event` function
that takes the raw inputs and returns a fully-shaped
`Event` wrapped in `Result`. The manager composes them
with the persistence flow (append + cache refresh).

The split mirrors the three "shapes" of continuity
events: tool usage (hashed params/result), entity
observation (PII-gated), and categorical slot choice
(no PII concern).

Why one builder per file? Each event type has its own
validation rules (the entity builder colocates the
PII gate; the tool builder colocs the "hash-only
params/result" contract; the category builder has
neither). Keeping each in its own module makes the
rule colocated with the event shape — no future
caller can construct a `continuity.entity_seen`
without going through the PII gate.
"""

from .category import build_category_chosen_event
from .entity import build_entity_seen_event
from .tool import build_tool_used_event

__all__ = [
    "build_category_chosen_event",
    "build_entity_seen_event",
    "build_tool_used_event",
]
