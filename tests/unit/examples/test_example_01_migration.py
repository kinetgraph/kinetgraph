# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Smoke tests for ``examples/01_llm_basic.py`` after
the migration to ``LiteLLMToolWorker`` (ADR-043).

The example must:

  - Import without raising (no syntax error; the
    ``LiteLLMToolWorker`` symbol resolves; the
    ``LLMConfig`` import resolves).
  - Construct a ``LiteLLMToolWorker`` and call
    ``await worker.invoke(...)`` with a fixed
    ``idempotency_key``; the call returns a
    ``Result[dict, Exception]`` (the new envelope).
  - Print the model / latency / tokens / cost / text
    on success.

The LLM call itself is MOCKED via the same pattern
as the ``test_litellm_worker`` tests: a fake
transport returns a fixed dict, the worker
translates it into the public envelope. The example
is loaded via ``importlib.util`` (the same pattern
as ``test_example_05b_shim.py``) so it can be
exercised without a real LLM / Redis.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples"


def _load_module(name: str) -> object:
    """Load an example as a module (the file is not
    on ``sys.path``; examples are run as scripts)."""
    spec = importlib.util.spec_from_file_location(name, _EXAMPLES_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_example_01_imports_without_legacy_litellm_tool() -> None:
    """The example no longer imports
    ``LiteLLMTool`` (the legacy Tool class, removed
    in v0.9.0 per ADR-043); it uses
    ``LiteLLMToolWorker`` instead.
    """
    # The module's source must mention the worker.
    src = (_EXAMPLES_DIR / "01_llm_basic.py").read_text()
    assert "LiteLLMToolWorker" in src
    assert "LiteLLMTool(" not in src, "example 01 still uses LiteLLMTool(...)"


def test_example_01_main_uses_litellm_tool_worker() -> None:
    """The example's ``main()`` builds a
    ``LiteLLMToolWorker`` and calls
    ``await worker.invoke(...)`` (the new worker
    pattern), with a deterministic
    ``idempotency_key`` and ``think=False`` for
    thinking Ollama models.
    """
    fake_completion = {
        "choices": [
            {
                "message": {"content": "Event Sourcing is..."},
                "finish_reason": "stop",
            }
        ],
        "model": "ollama/qwen3.5:4b",
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
        },
    }
    fake_transport = _async_return(fake_completion)
    with patch(
        "kntgraph.agents.tools.llm.LiteLLMTransportAdapter",
        return_value=fake_transport,
    ):
        with patch.dict(os.environ, {"KNT_LLM_DEFAULT_MODEL": "ollama/qwen3.5:4b"}):
            module = _load_module("01_llm_basic")
            asyncio.run(module.main())

    # The fake transport was called once.
    assert fake_transport.call_count == 1
    # The call's idempotency_key is the example's
    # stable prefix.
    call = fake_transport.call_args
    request = call.args[0]
    assert request.idempotency_key == "example-01:hello"


def _async_return(value):
    """Build an async callable that returns ``value``
    on every call. Mirrors the pattern in
    ``test_litellm_worker.py`` (the worker tests
    don't share a helper; each file builds its own
    AsyncMock-shaped return)."""
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=value)
