# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.tools.protocol -- framework-level Tool
Protocols (Iter 25).

The Tool concept is decomposed into **three layered
Protocols**, each capturing a distinct responsibility:

  - ``Describable`` -- identity. An object that can be
    inspected (``name``, ``description``,
    ``input_schema``) without being invoked. Used by
    registries, ACL, schemas, intent routers.

  - ``Callable[T_in, T_out]`` -- execution. An object
    that can be called with a payload. Independent of
    idempotency, railway, or event emission.

  - ``Tool[R, P]`` -- full orchestration. A sub-Protocol
    of ``Describable`` AND ``Callable`` that adds the
    ``idempotency_key`` keyword and the ``Result[R,
    ToolError]`` envelope. The shape the ``ToolInvoker``
    consumes.

Why three Protocols
-------------------

A single monolithic ``Tool`` Protocol forces every
consumer to implement every dimension. Registries only
need identity. Transports only need execution. The
``ToolInvoker`` needs the full contract. The layered
Protocols let each consumer ask for what it needs:

  - ``ToolRegistry`` accepts ``Describable``.
  - ``SolutionPromoter`` accepts a ``Callable`` (the
    redactor is a transformer, not a Tool).
  - ``ToolInvoker`` accepts a ``Tool``.

This also breaks the import cycle that Iter 25 is
closing: the promoter's redactor slot can be typed
as a ``Callable`` Protocol without importing
``kntgraph.agents.tools.pii`` (the concrete class), so the
load-time cycle through ``_promoter.py`` is gone.

The ``kntgraph.agents.tools.protocol`` module is a
re-export shim for backward compatibility.
"""

from __future__ import annotations

from typing import ParamSpec, Protocol, TypeVar, runtime_checkable

from kntgraph.core.result import Result, ToolError


__all__ = [
    "Callable",
    "Describable",
    "Tool",
    "ToolArgValue",
]


# ``ToolArgValue`` is the framework-level type for any
# value passed across the tool boundary (kwargs,
# results, schema fragments). Unbound on purpose: the
# framework does not inspect values, it just passes
# them through. Concrete tools specialise via ``cast``
# / explicit annotations inside their own ``invoke``
# bodies.
ToolArgValue = object


# ---------------------------------------------------------------------------
# Layer 1: Describable (identity)
# ---------------------------------------------------------------------------


@runtime_checkable
class Describable(Protocol):
    """
    An object that carries identity metadata.

    ``name``, ``description``, ``input_schema`` are
    the public surface that registries, intent
    routers, and schema validators read. No
    invocation is implied.

    A ``Describable`` is NOT necessarily executable.
    A tool schema descriptor (e.g. ``ToolDescriptor``)
    is a ``Describable`` even when the corresponding
    tool is no longer invocable in the running
    process.
    """

    name: str
    description: str
    input_schema: dict


# ---------------------------------------------------------------------------
# Layer 2: Callable (execution)
# ---------------------------------------------------------------------------


# ``T_in`` is the input payload type. ``T_out`` is the
# return value. Concrete callables specialise these via
# their own type annotations.
T_in = TypeVar("T_in")
T_out = TypeVar("T_out")


@runtime_checkable
class Callable(Protocol[T_in, T_out]):
    """
    An object that can be called asynchronously with a
    payload.

    Independent of identity, idempotency, or railway
    error envelopes. Pure execution.

    Examples that satisfy ``Callable`` but NOT ``Tool``:
      - ``LLMTransport.__call__(...)`` -- async I/O
        without idempotency_key.
      - A redactor ``redact(payload) -> result`` -- a
        pure transformer.
      - An embedding provider's ``embed(text) -> vec``.

    The Protocol is ``@runtime_checkable`` so
    ``isinstance(obj, Callable)`` works defensively in
    factories and tests. Sub-Protocols (e.g.
    ``LLMTransport``) inherit this; they must also be
    ``@runtime_checkable`` for the combined
    ``isinstance(obj, LLMTransport)`` check to work
    (Python's ``@runtime_checkable`` constraint).
    """

    async def __call__(self, payload: T_in) -> T_out: ...


# ---------------------------------------------------------------------------
# Layer 3: Tool (full orchestration)
# ---------------------------------------------------------------------------


# ``R`` is the concrete return type a tool promises on
# success. Concrete tools subclass / annotate their
# ``invoke`` with their own type (``dict``, ``None``,
# a dataclass, etc.) so consumers get precise types
# without falling back to ``Any`` / ``object``.
R = TypeVar("R")
# ``P`` captures the keyword-only parameter shape
# ``**kwargs``. Concrete tools re-parameterise it via
# the ``Self`` pattern (declared at their own class).
P = ParamSpec("P")


@runtime_checkable
class Tool(Describable, Protocol[R]):
    """
    A fully orchestrated tool.

    A ``Tool`` is a ``Describable`` (carries identity
    metadata) and adds the framework-level concerns
    on top of ``invoke``:

      1. ``idempotency_key`` is a required keyword
         argument on ``invoke``. The ``ToolInvoker``
         injects it from the ``event_id`` of the
         ``tool.<name>.requested`` event. Tools MUST
         accept it (non-idempotent tools MUST dedupe
         on it).
      2. The return type is a ``Result[R, ToolError]``,
         not a raw value. The ``ToolInvoker``
         translates the ``Err`` branch into a
         ``tool.<name>.failed`` event; the ``Ok``
         branch becomes a ``tool.<name>.completed``
         event.

    Sub-Protocol relationship
    -------------------------

    ``Tool`` IS-A ``Describable``: every Tool has a
    name, description, and input_schema. The
    ``invoke`` method is the execution surface; it is
    the same as the ``Callable`` Protocol's
    ``__call__`` semantically (an async call with a
    payload) but is named ``invoke`` for clarity at
    call sites.

    The relationship to ``Callable`` is
    **duck-typed**, not formally declared. A Tool can
    be used where a Callable is expected if the
    caller adapts ``invoke(**kwargs)`` to
    ``__call__(payload)`` at the boundary (a
    ``ToolAsCallable`` adapter, not built today).
    For most consumers, a Tool IS-A Describable and
    is invoked directly via ``invoke``.

    Idempotency contract
    --------------------

    The ``ToolInvoker`` injects an ``idempotency_key`` keyword
    argument into every ``invoke`` call. The key is the
    ``event_id`` (UUID) of the ``tool.{name}.requested``
    event that triggered the call. It is stable across
    re-dispatches: a reactive system that re-emits the same
    request (e.g. after a dispatcher restart) will produce
    the same key, and a tool that honors it can dedupe.

    Tools with non-idempotent side effects (bank transfers,
    payment captures, etc.) MUST implement dedup on this
    key. Tools with naturally idempotent behavior
    (read-only queries, idempotent API calls) may ignore it
    but should still accept the parameter for uniformity.

    The framework never reads the ``idempotency_key`` from
    the tool's return value; it is the tool's responsibility
    to persist the key → result mapping externally.
    """

    name: str
    description: str
    input_schema: dict

    async def invoke(
        self,
        *,
        idempotency_key: str,
        **kwargs: P.kwargs,
    ) -> Result[R, ToolError]: ...
