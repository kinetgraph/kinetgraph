# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the LLMTransport-as-Callable refactor
(Iter 28 FU 3 — roadmap item "LLMTransport refator
para Callable[..., dict]").

The framework's ``Callable[T_in, T_out]`` Protocol
(Iter 25) is the canonical shape for any async
executable. Before this refactor, ``LLMTransport``
was a custom Protocol with a ``complete()`` method —
duck-typed equivalent of ``__call__``, but not
formally related.

After the refactor, ``LLMTransport`` is a sub-Protocol
of ``Callable[LLMRequest, dict]``. The 9 keyword
parameters of ``complete()`` are bundled into an
``LLMRequest`` dataclass (immutable, frozen, slots).
This:

  1. Materializes the relationship between
     ``LLMTransport`` and the framework's
     ``Callable`` Protocol.
  2. Makes the request a first-class value (callers
     can pass it around, log it, inspect it).
  3. Aligns the LLM transport with the
     ``graph/graphrag/retriever.py`` pattern
     (search adapters) which already use the
     ``Callable`` shape.
  4. Removes the duck-typed gap documented in Iter 25
     ("the relationship to Callable is duck-typed, not
     formally declared").

The public surface (``LLMResponse``, ``LLMUsage``,
``LLMChunk``) is unchanged. The contract
(``complete() -> dict``) is unchanged in observable
behavior. Only the call signature changes:
``await transport.complete(model=..., messages=...)``
becomes ``await transport(LLMRequest(model=..., messages=...))``.
"""

from __future__ import annotations

import pytest


class TestLLMRequest:
    """The new ``LLMRequest`` value object bundles
    the 9 keyword parameters of ``complete()``."""

    def test_request_has_all_complete_kwargs(self) -> None:
        """The request carries every keyword the
        previous ``complete()`` accepted."""
        from kntgraph.tools.llm_transport import LLMRequest

        req = LLMRequest(
            model="gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            max_tokens=1024,
            response_format={"type": "json_object"},
            drop_unsupported_params=False,
            idempotency_key="evt-123",
        )
        assert req.model == "gpt-4"
        assert req.messages == [{"role": "user", "content": "hi"}]
        assert req.temperature == 0.7
        assert req.max_tokens == 1024
        assert req.response_format == {"type": "json_object"}
        assert req.drop_unsupported_params is False
        assert req.idempotency_key == "evt-123"

    def test_request_is_frozen(self) -> None:
        """The request is immutable (frozen, slots)."""
        from kntgraph.tools.llm_transport import LLMRequest

        req = LLMRequest(
            model="gpt-4",
            messages=[],
            temperature=0.0,
            max_tokens=1,
        )
        with pytest.raises((AttributeError, Exception)):
            req.model = "gpt-5"  # type: ignore[misc]

    def test_request_default_values(self) -> None:
        """The request's default values match the
        previous ``complete()`` defaults."""
        from kntgraph.tools.llm_transport import LLMRequest

        req = LLMRequest(
            model="gpt-4",
            messages=[],
            temperature=0.0,
            max_tokens=1,
        )
        # Defaults from the prior complete() signature.
        assert req.response_format is None
        assert req.drop_unsupported_params is True
        assert req.idempotency_key is None

    def test_request_accepts_extra_kwargs(self) -> None:
        """The request has an ``extra`` dict for
        provider-specific kwargs (mirrors the
        ``**kwargs`` on the previous complete())."""
        from kntgraph.tools.llm_transport import LLMRequest

        req = LLMRequest(
            model="gpt-4",
            messages=[],
            temperature=0.0,
            max_tokens=1,
            extra={"top_p": 0.9, "stop": ["\n"]},
        )
        assert req.extra == {"top_p": 0.9, "stop": ["\n"]}


class TestLLMTransportProtocol:
    """``LLMTransport`` is a Protocol with a single
    ``__call__`` method (duck-typed equivalent of
    ``Callable[LLMRequest, dict]``).

    The previous version of the test asserted
    ``issubclass(LLMTransport, Callable)``. Python's
    ``@runtime_checkable`` constraint forbids Protocol
    subclassing from a subscripted generic (it loses
    the ``_is_protocol`` flag). The refactored
    design declares ``LLMTransport`` as a fresh
    Protocol that **structurally** matches
    ``Callable[LLMRequest, dict]`` (both have the
    same ``__call__`` shape).
    """

    def test_protocol_has_call_method(self) -> None:
        """The Protocol declares ``__call__`` (the
        Callable shape)."""
        from kntgraph.tools.llm_transport import (
            LLMTransport,
        )

        assert "__call__" in dir(LLMTransport)

    def test_class_with_call_method_satisfies_protocol(self) -> None:
        """A class with the right ``__call__`` shape
        satisfies ``LLMTransport`` (structural)."""
        from kntgraph.tools.llm_transport import (
            LLMTransport,
            LLMRequest,
        )

        class _StubTransport:
            async def __call__(self, request: LLMRequest) -> dict:
                return {"choices": [{"message": {"content": "ok"}}]}

        assert isinstance(_StubTransport(), LLMTransport)


class TestLLMTransportBackwardCompat:
    """The refactor preserves the existing tests'
    expectations. The previous ``complete()`` method is
    removed; callers use ``__call__`` directly.

    Iter 28 FU 3 breaks the public surface of
    ``LLMTransport`` (callers must migrate to the
    new shape). The migration is mechanical: every
    ``transport.complete(**kwargs)`` becomes
    ``transport(LLMRequest(**kwargs))``.
    """

    def test_old_complete_method_does_not_exist(self) -> None:
        """The previous ``complete()`` method is
        gone. Callers must use ``__call__``."""
        from kntgraph.tools.llm_transport import (
            LLMTransport,
        )

        # The Protocol declares ``__call__``; the
        # old ``complete`` attribute is gone.
        assert not hasattr(LLMTransport, "complete")

    def test_call_signature_is_single_request(self) -> None:
        """The Protocol's ``__call__`` takes a single
        ``LLMRequest`` (not 9 keyword arguments)."""

        from kntgraph.tools.llm_transport import (
            LLMTransport,
        )

        # Inspect the Protocol's __call__ signature.
        # The Protocol's body is in
        # ``LLMTransport.__call__``; we read the
        # annotations.
        annotations = LLMTransport.__call__.__annotations__
        # Single positional parameter: ``request``.
        assert "request" in annotations
