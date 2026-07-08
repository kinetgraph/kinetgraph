# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``LLMTransport`` -- framework-owned Protocol for any
LLM I/O boundary.

The framework (this module) defines the shape of an
async completion call and the value objects that flow
through it. Concrete transports (``LiteLLMTransportAdapter``,
custom HTTP clients, mocks) are pluggable and may live
in any client package that depends on ``kntgraph``
-- the canonical LiteLLM-based transport lives in
``kntgraph.agents.tools.llm``.

Iter 28 FU 3: ``LLMTransport`` is now a sub-Protocol
of ``Callable[LLMRequest, dict]`` (the framework's
generic async-execution shape, introduced in Iter 25).
The previous custom ``complete()`` method is replaced
by ``__call__(request: LLMRequest) -> dict``. The 9
keyword parameters of the old ``complete()`` are
bundled into the ``LLMRequest`` dataclass.

This change:
  - Materializes the relationship between
    ``LLMTransport`` and ``Callable`` (previously
    duck-typed; now formal).
  - Makes the request a first-class value (callers
    can pass it around, log it, inspect it).
  - Aligns the LLM transport with the
    ``graph/graphrag/retriever.py`` pattern (search
    adapters) which already use the ``Callable``
    shape.
  - Removes the duck-typed gap documented in
    Iter 25 §1 ("the relationship to ``Callable`` is
    duck-typed, not formally declared").

Public surface
--------------

  - ``LLMTransport``: sub-Protocol of
    ``Callable[LLMRequest, dict]``.
  - ``LLMRequest``: the value object that bundles the
    9 keyword parameters of the old ``complete()``
    method.
  - ``LLMResponse``, ``LLMUsage``, ``LLMChunk``:
    unchanged from the previous design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from kntgraph.core._typing import JsonValue


__all__ = [
    "LLMChunk",
    "LLMRequest",
    "LLMResponse",
    "LLMTransport",
    "LLMUsage",
]


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token usage extracted from an LLM response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """
    Result of a single completion call.

    `text` is the content of the first choice. For
    multi-choice or multi-message, use `raw` (the
    original response dict, LiteLLM-style).

    `cost_usd` is populated when the client can compute
    it (e.g. via `litellm.completion_cost`); it may be
    `None` for local or unknown models.
    """

    text: str
    model: str
    usage: LLMUsage
    latency_ms: float
    cost_usd: Optional[float] = None
    finish_reason: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMChunk:
    """One chunk of a streaming response."""

    delta: str
    model: str
    finish_reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """
    The value object passed to ``LLMTransport.__call__``.

    Iter 28 FU 3: the 9 keyword parameters of the
    previous ``complete()`` method are bundled into
    this dataclass. ``LLMRequest`` is immutable
    (frozen + slots), so callers can pass it around
    safely; the transport cannot mutate the request
    after receiving it.

    The ``extra`` dict carries provider-specific kwargs
    (the ``**kwargs`` of the previous ``complete()``).
    Concrete transports extract what they need (e.g.
    ``top_p``, ``stop``, ``tools``); unused keys are
    ignored.
    """

    model: str
    messages: list[dict]
    temperature: float
    max_tokens: int
    response_format: Optional[dict] = None
    drop_unsupported_params: bool = True
    idempotency_key: Optional[str] = None
    extra: "dict[str, JsonValue]" = field(default_factory=dict)


@runtime_checkable
class LLMTransport(Protocol):
    """
    Async transport that turns a completion request into
    a raw response dict (LiteLLM-style by convention,
    but the contract is shape-only -- any compatible
    response can be passed to the Tool's response
    adapter).

    Iter 28 FU 3: ``LLMTransport`` IS-A ``Callable``
    semantically (it has a single ``__call__`` method
    that takes a payload and returns a result). The
    Protocol is declared as a fresh Protocol (not a
    subclass of ``Callable``) because Python's
    ``@runtime_checkable`` constraint requires every
    base to be a ``Protocol``, and inheriting from
    ``Callable[LLMRequest, dict]`` (a subscripted
    generic) breaks that invariant.

    Concrete implementations provide a single
    ``async def __call__(self, request: LLMRequest)
    -> dict`` method. They also satisfy
    ``Callable[LLMRequest, dict]`` by structural
    typing -- the framework treats them as drop-in
    ``Callable`` instances via
    ``isinstance(obj, Callable)`` (which is the
    framework's contract for any async executable;
    see ``protocol.py::Callable``).

    Implementations
    ---------------

      - ``kntgraph.agents.tools.llm.LiteLLMTransportAdapter``:
        real LiteLLM call.
      - ``kntgraph.agents.tools.cache.CachingLLMTransport``:
        decorator that memoizes by ``idempotency_key``.
      - Test doubles (e.g. ``FakeLLMTransport``): for
        unit tests.

    The Protocol is ``@runtime_checkable`` so factories
    and tests can use ``isinstance(obj, LLMTransport)``
    for defensive type checks (same pattern as the
    framework's Redis shards -- ADR-019).

    Contract
    --------

    ``__call__(request)`` is async and returns a dict
    with at least:

      - ``choices``: list, first element has ``.message.content``
        and ``.finish_reason``.
      - ``usage``: dict with ``prompt_tokens``,
        ``completion_tokens``, ``total_tokens``.
      - ``model``: str (the actual model used; may differ
        from requested on routing).

    The transport is responsible for raising rate-limit,
    auth, and timeout errors as exceptions. The Tool
    translates them into ``Result`` outcomes.
    """

    async def __call__(self, request: LLMRequest) -> dict: ...
