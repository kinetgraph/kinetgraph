# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression test: the ``LLMClient`` facade has been
**deleted** (Iter 28 FU 6). The facade had 0 callers
production and was inconsistent with the post-Iter 28
FU 3 ``LLMTransport`` shape (``__call__``, not
``complete``). Callers that needed a low-level adapter
already had direct access to ``LiteLLMTransportAdapter``.

This test is the deletion gate. If a future refactor
re-introduces ``LLMClient`` (or its supporting
``_llm_client.py`` module), this test fails.

Background
----------

Iter 18b (ADR-019 epílogo) introduced ``LLMClient``
as a facade following the same pattern as
``GraphPool`` (graph) and ``EmbeddingClient``
(embedding). The pattern was:

  - ``XxxTransport`` -- Protocol
  - ``XxxTransportAdapter`` -- low-level adapter
  - ``XxxClient`` -- facade
  - ``XxxTool`` -- orchestrator

Iter 28 FU 3 (ADR-030) migrated ``LLMTransport`` from
``async def complete(...)`` to
``async def __call__(LLMRequest) -> dict`` (structural
match with ``Callable[LLMRequest, dict]``). The
``LLMClient.complete(...)`` delegation became
inconsistent with the Protocol: ``LLMClient`` declares
``complete`` (legacy) but not ``__call__`` (current).
The Protocol's ``__call__`` (inherited as a method
attribute, not implemented) is a no-op.

The facade had 0 callers in production code (apps,
examples, tests) outside of its own test file
(``test_llm_client_guard.py``). The facade's only
purpose was to test the lazy import of the default
adapter — a feature without a user.

The structural-equivalent of the facade, post-Iter 28
FU 3, is one of:

  - ``LLMTransport`` Protocol (framework primitive,
    no default impl).
  - ``LiteLLMTransportAdapter`` (concrete, in
    ``kntgraph.agents.tools.llm``, requires ``litellm``).
  - ``CachingLLMTransport`` (decorator, in
    ``kntgraph.agents.tools.cache``).

Application code that needs a transport picks one
based on its needs; there is no general-purpose
default facade. The canonical reference is the
``_FakeAdapter`` in ``test_llm_client_guard.py`` (now
deleted): tests construct an ad-hoc transport.

Migration
---------

Zero internal callers. External consumers (if any) can
construct ``LiteLLMTransportAdapter()`` directly to
get the same behaviour the default branch of
``LLMClient.__init__`` produced.
"""

from __future__ import annotations

import pytest


class TestLLMClientDeleted:
    """The ``LLMClient`` facade is GONE. Callers use
    ``LiteLLMTransportAdapter`` directly (or any
    ``LLMTransport`` impl)."""

    def test_llm_client_not_exported_from_tools(self) -> None:
        """``from kntgraph.agents.tools import LLMClient``
        must fail with ``ImportError``.

        Before this iter, ``LLMClient`` was a public
        symbol in ``kntgraph.agents.tools.__all__``. After
        this iter, it is gone.
        """
        with pytest.raises(ImportError):
            from kntgraph.agents.tools import LLMClient  # noqa: F401

    def test_llm_client_module_deleted(self) -> None:
        """The supporting ``_llm_client.py`` module is
        gone. Catches both
        ``from kntgraph.agents.tools._llm_client import ...``
        and ``kntgraph.agents.tools._llm_client`` attribute
        access patterns."""
        with pytest.raises((ModuleNotFoundError, ImportError)):
            __import__("kntgraph.agents.tools._llm_client", fromlist=["*"])

    def test_llm_client_not_in_tools_all(self) -> None:
        """``LLMClient`` is not in
        ``kntgraph.agents.tools.__all__``."""
        import kntgraph.agents.tools as tools_mod

        assert not hasattr(tools_mod, "LLMClient"), (
            "LLMClient should be deleted from kntgraph.agents.tools"
        )
        assert "LLMClient" not in tools_mod.__all__

    def test_guard_test_deleted(self) -> None:
        """The supporting test file
        ``test_llm_client_guard.py`` is gone (it
        exercised a feature without a user)."""
        from pathlib import Path

        # Resolve workspace root: this file lives at
        # ``kntgraph/tests/unit/``; the deleted test
        # lived at ``kntgraph.agents/tests/unit/tools/``.
        workspace_root = Path(__file__).parent.parent.parent
        test_path = (
            workspace_root
            / "kntgraph.agents"
            / "tests"
            / "unit"
            / "tools"
            / "test_llm_client_guard.py"
        )
        assert not test_path.exists(), f"test file should be deleted: {test_path}"

    def test_litellm_transport_adapter_still_works(self) -> None:
        """The low-level ``LiteLLMTransportAdapter`` is
        the canonical replacement for ``LLMClient()``
        (the default-branch behaviour). It is importable
        from ``kntgraph.agents.tools.llm``."""
        from kntgraph.tools.llm_transport import LLMTransport

        from kntgraph.agents.tools.llm import LiteLLMTransportAdapter

        assert issubclass(LiteLLMTransportAdapter, LLMTransport)

    def test_llm_transport_protocol_still_works(self) -> None:
        """The framework's ``LLMTransport`` Protocol
        (the public surface) is still importable."""
        from kntgraph.tools import llm_transport as mod
        from kntgraph.tools.llm_transport import (
            LLMRequest,
            LLMTransport,
        )

        # The Protocol still declares ``__call__`` (the
        # canonical contract post-Iter 28 FU 3).
        assert hasattr(LLMTransport, "__call__")
        # ``LLMRequest`` lives in the same module as
        # ``LLMTransport`` — the framework primitive.
        assert LLMRequest.__module__ == mod.__name__
