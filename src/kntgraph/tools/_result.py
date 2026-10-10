# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``ToolResult`` -- the result of one ``@tool_worker`` invocation
(ADR-079).

A frozen dataclass with a discriminated ``status`` field. Replaces
the legacy ``dict[str, str | JsonValue]`` ``result_dict`` that
every dispatch path used to return.

The dataclass form buys:

- **Type-narrowed discriminators.** ``if result.status == "ok":``
  makes ``result.value`` accessible under mypy/pyright; the
  ``else:`` branch makes ``result.error`` accessible. The legacy
  dict form was cego.
- **Attribute access.** ``result.status`` instead of
  ``result_dict["status"]``.
- **Immutability.** ``frozen=True`` enforces it at the type
  level and at runtime; no ``MappingProxyType`` is needed.
- **One wire boundary.** ``to_wire()`` is the only place a dict
  literal is admitted (ADR-079 §3.3 + §5 anti-patterns allowlist).

Construction
------------

Use the factories ``ToolResult.ok(value)`` and
``ToolResult.err(error)``. The dataclass constructor ``cls(...)``
is also public; the factories are the recommended form for
readability.

Wire boundary
-------------

``to_wire()`` returns a ``Mapping[str, str | JsonValue]`` that
matches the canonical shape on the Redis / EventLog wire
(``tool.<name>.completed`` event payload is built from this).
The ``{}`` literal lives **inside** ``to_wire()`` and nowhere
else (ADR-079 §5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from kntgraph.core._typing import JsonValue

__all__ = ["ToolResult"]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result of one ``@tool_worker`` invocation.

    Discriminated union over ``status``:

    - ``status == "ok"`` ⇒ ``value`` is the tool's
      ``Result.unwrap()`` payload (serialisable to ``JsonValue``
      per the EventLog wire shape).
    - ``status == "err"`` ⇒ ``error`` is the ``str(...)`` of the
      tool's ``Result.err_value_or_raise()``.

    The on-wire shape (Redis / EventLog) is produced by
    ``to_wire()`` and is the only site in framework code that
    admits a dict literal (ADR-079 §3.3, §5 allowlist).
    """

    status: Literal["ok", "err"]
    value: JsonValue | None = None
    error: str | None = None

    def to_wire(self) -> Mapping[str, str | JsonValue]:
        """Project the dataclass to the wire shape used by
        ``build_completion_event`` and ``build_failure_event``.

        Returns a fresh ``Mapping`` per call; callers that need
        to pass it through Redis serialise it via the
        infrastructure adapter (see ADR-067 §1.1).
        """
        if self.status == "ok":
            return _ok_wire(self.value)
        return _err_wire(self.error)

    @classmethod
    def ok(cls, value: JsonValue) -> ToolResult:
        """Build a success result. ``value`` is the tool's
        ``Result.unwrap()`` payload."""
        return cls(status="ok", value=value, error=None)

    @classmethod
    def err(cls, error: str) -> ToolResult:
        """Build a failure result. ``error`` is the ``str(...)``
        of the tool's ``Result.err_value_or_raise()``."""
        return cls(status="err", value=None, error=error)


def _ok_wire(value: JsonValue | None) -> Mapping[str, str | JsonValue]:
    # ADR-079 §3.3: this is the one boundary that admits a
    # dict literal — the wire conversion is the role of
    # ``to_wire()``.
    return {"status": "ok", "value": value}


def _err_wire(error: str | None) -> Mapping[str, str | JsonValue]:
    return {"status": "err", "error": error}
