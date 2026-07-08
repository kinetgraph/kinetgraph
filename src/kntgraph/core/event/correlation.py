# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event.correlation -- Distributed tracing context (contextvar-based, async-safe).

Three classes that compose the correlation layer:

  - `CorrelationContext`: an immutable, frozen dataclass
    holding `correlation_id`, `causation_id`, `span_id`,
    and a metadata dict. Pure value object.

  - `CorrelationMiddleware`: the async-safe carrier
    (ContextVar-backed). Owns `start`, `continue_from`,
    `current`, `clear`, and `scope`. The module exposes
    a singleton `correlation_middleware` that the rest
    of the framework uses.

  - `CorrelationScope`: a context-manager wrapper around
    `Middleware.start` / `Middleware.clear`. Lets a
    caller scope a `correlation_id` to a `with` block
    without leaking it past the block.

The ContextVar (`_correlation_context`) is module-private
to the package — other code reaches the current context
through `correlation_middleware.current()`.

Dependency note: `CorrelationMiddleware.continue_from`
takes an `Event`, but `Event` lives in
``event.py`` and imports `CorrelationContext` here. To
avoid an import cycle, `continue_from` does a local
import of `Event` (only when called). `Event.from_dict`
likewise does a local import of `CorrelationContext`
when it needs it.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Optional

from .._typing import JsonValue
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from .event import Event


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """
    Distributed tracing context.

    `correlation_id` is the flow id (stable across the whole flow).
    `causation_id` is the immediate parent event id (causal chain).
    `span_id` is a per-operation id (OpenTelemetry-compatible).
    """

    correlation_id: UUID
    causation_id: Optional[UUID] = None
    span_id: Optional[UUID] = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        metadata: Optional[Mapping[str, JsonValue]] = None,
        correlation_id: Optional[UUID] = None,
        causation_id: Optional[UUID] = None,
        span_id: Optional[UUID] = None,
    ) -> "CorrelationContext":
        return cls(
            correlation_id=correlation_id or uuid4(),
            causation_id=causation_id,
            span_id=span_id or uuid4(),
            metadata=dict(metadata) if metadata else {},
        )

    def to_dict(self) -> dict:
        return {
            "correlation_id": str(self.correlation_id),
            "causation_id": str(self.causation_id) if self.causation_id else "",
            "span_id": str(self.span_id) if self.span_id else "",
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CorrelationContext":
        return cls(
            correlation_id=UUID(data["correlation_id"]),
            causation_id=UUID(data["causation_id"])
            if data.get("causation_id")
            else None,
            span_id=UUID(data["span_id"]) if data.get("span_id") else None,
            metadata=dict(data.get("metadata") or {}),
        )


_correlation_context: contextvars.ContextVar[Optional[CorrelationContext]] = (
    contextvars.ContextVar("fmh_correlation", default=None)
)


class CorrelationMiddleware:
    """
    Async-safe carrier for the current correlation context.

    Usage:

        with correlation_middleware.scope(metadata={"flow": "x"}):
            # any Event.create(...) called here will inherit the context
            ...
    """

    def start(
        self,
        metadata: Optional[Mapping[str, JsonValue]] = None,
        correlation_id: Optional[UUID] = None,
        causation_id: Optional[UUID] = None,
        span_id: Optional[UUID] = None,
    ) -> CorrelationContext:
        ctx = CorrelationContext.new(
            metadata=metadata,
            correlation_id=correlation_id,
            causation_id=causation_id,
            span_id=span_id,
        )
        _correlation_context.set(ctx)
        return ctx

    def continue_from(
        self,
        cause: "Event",
        span_id: Optional[UUID] = None,
    ) -> CorrelationContext:
        ctx = CorrelationContext(
            correlation_id=cause.correlation.correlation_id,
            causation_id=cause.event_id,
            span_id=span_id or uuid4(),
            metadata=cause.correlation.metadata,
        )
        _correlation_context.set(ctx)
        return ctx

    def current(self) -> Optional[CorrelationContext]:
        return _correlation_context.get()

    def clear(self) -> None:
        _correlation_context.set(None)

    def scope(
        self,
        metadata: Optional[Mapping[str, JsonValue]] = None,
    ) -> "CorrelationScope":
        return CorrelationScope(self, metadata)


class CorrelationScope:
    def __init__(
        self,
        middleware: CorrelationMiddleware,
        metadata: Optional[Mapping[str, JsonValue]],
    ) -> None:
        self._mw = middleware
        self._metadata = metadata
        self._ctx: Optional[CorrelationContext] = None

    def __enter__(self) -> CorrelationContext:
        self._ctx = self._mw.start(self._metadata)
        return self._ctx

    def __exit__(self, exc_type, exc, tb) -> None:
        self._mw.clear()


correlation_middleware = CorrelationMiddleware()


__all__ = [
    "CorrelationContext",
    "CorrelationMiddleware",
    "CorrelationScope",
    "correlation_middleware",
]
