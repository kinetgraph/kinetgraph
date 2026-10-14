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
- **One wire boundary.** ``to_wire()`` is the place where a
  *populated* dict literal (``{"status": "ok", "value": value}``)
  is admitted (ADR-079 §3.3 + §5). Empty ``{}`` sentinels are
  also allowed elsewhere in framework code (ADR-079 §5).

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
from kntgraph.core.result.errors import ToolErrorClass

__all__ = ["ToolResult"]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result of one ``@tool_worker`` invocation.

    Discriminated union over ``status``:
      - ``"ok"`` ⇒ ``value`` is the tool's payload.
      - ``"err"`` ⇒ ``error`` is the human-readable
        message and ``error_class`` is the framework
        classification (transient / permanent / unknown).

    ``error_class`` lets the WorkerManager, TTL sweeper,
    and circuit breaker act on the failure type without
    parsing the free-form ``error`` string. ``transient``
    = retry may succeed; ``permanent`` = will never succeed
    (framework MUST NOT re-dispatch); ``unknown`` = legacy
    safe default.

    Wire shape: ``to_wire()`` (ADR-079 §3.3, §5 allowlist).
    """

    status: Literal["ok", "err"]
    value: JsonValue | None = None
    error: str | None = None
    error_class: ToolErrorClass = "unknown"

    def to_wire(self) -> Mapping[str, str | JsonValue]:
        """Project the dataclass to the wire shape used by
        ``build_completion_event`` and ``build_failure_event``.
        On the ``err`` branch the wire carries both
        ``error`` (human-readable) and ``error_class``
        (canonical classification).
        """
        if self.status == "ok":
            return _ok_wire(self.value)
        return _err_wire(self.error, self.error_class)

    @classmethod
    def ok(cls, value: JsonValue) -> ToolResult:
        """Build a success result. ``value`` is the tool's
        ``Result.unwrap()`` payload."""
        return cls(status="ok", value=value, error=None)

    @classmethod
    def err(
        cls,
        error: str,
        *,
        error_class: ToolErrorClass = "unknown",
    ) -> ToolResult:
        """Build a failure result.

        ``error_class`` classifies the failure for
        downstream consumers (TTL sweeper, circuit
        breaker). Use ``ToolError.transient`` /
        ``ToolError.permanent`` factories when raising
        in tool code so the classification survives the
        ``Result`` → ``ToolResult`` conversion.
        """
        return cls(status="err", value=None, error=error, error_class=error_class)


def _ok_wire(value: JsonValue | None) -> Mapping[str, str | JsonValue]:
    # ADR-079 §3.3: this is the one boundary that admits a
    # dict literal — the wire conversion is the role of
    # ``to_wire()``.
    return {"status": "ok", "value": value}


def _err_wire(
    error: str | None,
    error_class: ToolErrorClass,
) -> Mapping[str, str | JsonValue]:
    return {"status": "err", "error": error, "error_class": error_class}
