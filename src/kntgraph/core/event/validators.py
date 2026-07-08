# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event.validators -- Runtime guards for Event fields.

The static `Literal` types on `Event` are static-only
contracts. The runtime guards in this module are what
protect us from corrupted wire data and buggy producers
emitting empty strings, non-Mapping payloads, or
malformed timestamps.

All helpers raise the documented exception type
(`TypeError` for wrong shape, `ValueError` for wrong
content). The `Event` class and its builders call these
helpers before construction so the error fires at the
boundary, not deeper in the framework.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """Timezone-aware UTC `datetime` (the framework's
    canonical timestamp source)."""
    return datetime.now(timezone.utc)


def validate_event_type(event_type: Any) -> None:
    """
    Runtime guard for `event_type` — the wire-level
    `str` identifier that names what the event means
    (e.g. "agent.spawned", "task.completed"). The
    static `Literal` type is a static-only promise;
    this function is what protects us from corrupted
    Redis entries and buggy producers emitting empty
    or non-string types.

    Raises
    ------

      `TypeError` if `event_type` is not a `str`.
      `ValueError` if `event_type` is empty or
      contains only whitespace.

    Both messages mention `event_type` so callers can
    pinpoint the field from the exception text.
    """
    if not isinstance(event_type, str):
        raise TypeError(
            f"event_type must be str, got {type(event_type).__name__}: {event_type!r}"
        )
    if not event_type or not event_type.strip():
        raise ValueError(f"event_type must be a non-empty string, got {event_type!r}")


def validate_data(data: Any) -> None:
    """
    Runtime guard for `data` — the event payload. Must
    be either `None` (treated as empty) or a `Mapping`
    (dict-like). A bare string or list would silently
    break JSON serialisation later in the wire path.

    The parameter is typed ``Any`` because the validator
    only checks structural shape (isinstance(Mapping));
    the producer-facing type (``Mapping[str, JsonValue]``
    on ``Event.data``) is tighter and is enforced at the
    dataclass boundary. Accepting ``Any`` here lets the
    same validator run on wire-decoded dicts without
    forcing a cast.

    Raises
    ------

      `TypeError` if `data` is not `None` and not a
      `Mapping`.
    """
    if data is None:
        return
    if not isinstance(data, Mapping):
        raise TypeError(
            f"data must be a Mapping or None, got {type(data).__name__}: {data!r}"
        )


__all__ = ["utcnow", "validate_data", "validate_event_type"]
