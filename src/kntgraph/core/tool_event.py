# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.tool_event -- inverse of ``tools.protocol.ToolEventType``.

The constructor (``ToolEventType.requested(name)``,
``.completed(name)``, ...) builds the event-type string for a
given tool and kind. This module does the **reverse**:
given an event-type string, extract the kind and tool
name, or report that the string is not a tool event at
all.

Centralising the parse here kills the four near-identical
implementations that grew in:

  - ``kntgraph.agents/tools/invoker/_invoker.py`` — ``_tool_name_from_request``
  - ``kntgraph.agents/memory/solutions/_extractor.py`` (and siblings) — ``_tool_name_from_event_type`` +
    ``_is_tool_requested``/``.completed``/``.failed``
  - ``knowledge/falkordb/adapter.py`` — two inline
    ``startswith("tool.") and endswith(".completed"|.failed)``
  - ``kntgraph.agents/memory/solutions/__init__.py`` — public wrappers
    ``is_tool_requested``/``.completed``/``.failed`` that
    duplicate the private ones

The wire contract is a single line:

    tool.<name>.<kind>

where ``<name>`` may contain dots (e.g. ``invoice.issue``)
and ``<kind>`` is one of:

    requested | completed | failed | args_invalid

The kind names match ``ToolEventType`` (constructor) and
the convention documented in ADR-013 §2.2.
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple, Optional


class ToolEventKind(str, Enum):
    """
    The four states a tool call can reach in the event log.

    The string values are the trailing suffix of the
    event-type string (after ``tool.<name>.``); they match
    the constructor methods on ``tools.protocol.ToolEventType``.
    """

    REQUESTED = "requested"
    COMPLETED = "completed"
    FAILED = "failed"
    ARGS_INVALID = "args_invalid"


class ToolEvent(NamedTuple):
    """
    Parsed tool event. ``tool_name`` preserves any dots in
    the original name (``invoice.issue`` survives intact);
    ``kind`` is the terminal lifecycle state.
    """

    tool_name: str
    kind: ToolEventKind


# Wire prefixes / suffixes. Centralised so call-sites
# don't drift.
_TOOL_PREFIX = "tool."
_KIND_SUFFIXES: dict[str, ToolEventKind] = {
    ".requested": ToolEventKind.REQUESTED,
    ".completed": ToolEventKind.COMPLETED,
    ".failed": ToolEventKind.FAILED,
    ".args_invalid": ToolEventKind.ARGS_INVALID,
}


def parse_tool_event(event_type: str) -> Optional[ToolEvent]:
    """
    Parse ``tool.<name>.<kind>`` into a ``ToolEvent``.

    Returns ``None`` when the string is not a recognised
    tool event (i.e. does not start with ``tool.`` or the
    suffix is not one of the four kinds). Callers that
    need to distinguish "not a tool event" from "malformed
    tool event" should use :func:`is_tool_event` (faster
    predicate) or check the return value.

    The tool name can contain dots (the slicing
    ``[len(prefix):-len(suffix)]`` preserves them — see
    the ADR-013 example ``tool.invoice.issue.requested``).
    """
    if not event_type.startswith(_TOOL_PREFIX):
        return None
    for suffix, kind in _KIND_SUFFIXES.items():
        if event_type.endswith(suffix):
            tool_name = event_type[len(_TOOL_PREFIX) : -len(suffix)]
            if not tool_name:
                # "tool..requested" — empty name.
                return None
            return ToolEvent(tool_name=tool_name, kind=kind)
    return None


def tool_name_of(event_type: str) -> Optional[str]:
    """
    Return the tool name from a tool event-type string,
    or ``None`` if the string is not a tool event.

    Equivalent to ``parse_tool_event(event_type).tool_name``
    but skips the kind dispatch for the common case where
    only the name is needed.
    """
    if not event_type.startswith(_TOOL_PREFIX):
        return None
    for suffix in _KIND_SUFFIXES:
        if event_type.endswith(suffix):
            tool_name = event_type[len(_TOOL_PREFIX) : -len(suffix)]
            return tool_name or None
    return None


def is_tool_event(event_type: str, *kinds: ToolEventKind) -> bool:
    """
    Predicate: is ``event_type`` a tool event, optionally
    restricted to one or more kinds?

    Examples
    --------

    ``is_tool_event(e.event_type)`` — any tool event.

    ``is_tool_event(e.event_type, ToolEventKind.COMPLETED)``
    — only ``tool.<name>.completed`` events.

    Empty ``kinds`` means "any kind" (i.e. any of the
    four suffixes). This is the common case; the variadic
    exists for the read-paths that only care about
    terminal events (``.completed``/``.failed``).
    """
    if not event_type.startswith(_TOOL_PREFIX):
        return False
    for suffix, kind in _KIND_SUFFIXES.items():
        if event_type.endswith(suffix):
            if not kinds:
                return True
            return kind in kinds
    return False


__all__ = [
    "ToolEvent",
    "ToolEventKind",
    "is_tool_event",
    "parse_tool_event",
    "tool_name_of",
]
