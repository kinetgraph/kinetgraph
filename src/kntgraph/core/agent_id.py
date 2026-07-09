# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.agent_id -- the canonical trust boundary for
``agent_id`` and similar producer/tenant identifiers.

The pattern ``[A-Za-z0-9._:-]{1,128}`` was previously
declared in three places:

  - ``core/event.py`` (``_AGENT_ID_RE`` + ``_validate_agent_id``)
  - ``stream/event_log.py`` (``_AGENT_ID_RE_FOR_REDIS`` +
    ``_validate_agent_id_for_redis``)
  - ``resilience/bulkhead.py`` (``_BULKHEAD_KEY_RE``)

All three encoded the same wire-level invariant: the
producer identifier flows directly into Redis Stream
keys (``fmh:agents:{agent_id}:events``), bulkhead names,
and the FalkorDB tenant-id namespace. A character
outside ``[A-Za-z0-9._:-]`` could collide with the key
namespace or break SCAN patterns.

Centralising here:

  - ``MAX_AGENT_ID_LEN = 128`` -- the magic number
    made explicit.
  - ``AGENT_ID_RE`` -- the compiled pattern.
  - ``validate_agent_id(agent_id)`` -- returns
    ``None`` if valid, a short error string otherwise.
    Used at the Redis write seam (the EventLog) where
    the error must be embedded in a ``PersistenceError``
    detail.
  - ``assert_valid_agent_id(agent_id)`` -- raises
    ``TypeError``/``ValueError``. Used in
    ``Event.__post_init__`` and any other in-process
    producer where the type system can carry the
    failure.

The validator messages are split: the ``validate_*``
flavour returns **short** error strings (suitable for
``PersistenceError`` detail without leaking the raw
value); the ``assert_*`` flavour raises with the full
input in the message (for in-process debugging where
the caller is trusted).

Backwards compatibility
-----------------------

The previous private names are kept as aliases on the
modules that exported them, so downstream code that
imports ``kntgraph.core.event._AGENT_ID_RE`` or
``kntgraph.resilience.bulkhead._BULKHEAD_KEY_RE``
keeps working. New code should use the public names
from this module.
"""

from __future__ import annotations

import re

from ._typing import ValidatorInput


# Trust boundary constants.
MAX_AGENT_ID_LEN: int = 128
AGENT_ID_RE: re.Pattern[str] = re.compile(rf"^[A-Za-z0-9._:-]{{1,{MAX_AGENT_ID_LEN}}}$")


def validate_agent_id(agent_id: ValidatorInput) -> str | None:
    """
    Return ``None`` if ``agent_id`` is valid, otherwise
    a short error string suitable for embedding in a
    ``PersistenceError`` detail.

    The error string is **deliberately short** — it does
    not include the raw input. Operators that need the
    raw value log it separately at ERROR level
    (see ``EventLog.append``).

    Mirrors ``Event.__post_init__``'s validation but with
    a transport-friendly error shape.
    """
    if not isinstance(agent_id, str):
        return f"agent_id must be str, got {type(agent_id).__name__}"
    if not agent_id or not agent_id.strip():
        return "agent_id must be a non-empty string"
    if len(agent_id) > MAX_AGENT_ID_LEN:
        return f"agent_id too long ({len(agent_id)} chars)"
    if not AGENT_ID_RE.match(agent_id):
        return "agent_id contains characters outside [A-Za-z0-9._:-]"
    return None


def assert_valid_agent_id(agent_id: ValidatorInput) -> None:
    """
    Raise ``TypeError``/``ValueError`` if ``agent_id`` is
    invalid; return ``None`` if it is valid.

    Use this in in-process producers (``__post_init__``,
    ``from_dict``) where the type system can carry the
    failure. Use :func:`validate_agent_id` at transport
    seams (Redis writes) where the error must be
    serialised.
    """
    if not isinstance(agent_id, str):
        raise TypeError(
            f"agent_id must be str, got {type(agent_id).__name__}: {agent_id!r}"
        )
    if not agent_id or not agent_id.strip():
        raise ValueError(f"agent_id must be a non-empty string, got {agent_id!r}")
    if len(agent_id) > MAX_AGENT_ID_LEN:
        raise ValueError(
            f"agent_id must be <= {MAX_AGENT_ID_LEN} chars, "
            f"got {len(agent_id)} chars: {agent_id!r}"
        )
    if not AGENT_ID_RE.match(agent_id):
        raise ValueError(f"agent_id must match {AGENT_ID_RE.pattern}, got {agent_id!r}")


__all__ = [
    "AGENT_ID_RE",
    "MAX_AGENT_ID_LEN",
    "assert_valid_agent_id",
    "validate_agent_id",
]
