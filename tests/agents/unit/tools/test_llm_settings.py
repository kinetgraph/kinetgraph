# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests verifying that ``LiteLLMTool.__init__`` reads
its defaults from ``Settings`` (Iter 20).

Before Iter 20, the tool hard-coded ``default_model="gpt-4o-mini"``
and ``timeout_s=30.0`` as ``__init__`` defaults. Operators
who wanted to tune per-deployment had to subclass
the Tool.

After Iter 20, the tool's defaults are read from
``Settings()``. Tests verify:

  1. The factory ``LiteLLMTool()`` with no args reads
     ``settings.llm_default_model`` and
     ``settings.llm_timeout``.
  2. An explicit ``default_model=...`` arg still wins
     over Settings (the operator can override on a
     per-tool basis).
  3. The same applies to ``timeout_s``,
     ``max_cost_usd``, ``temperature``, ``max_tokens``.

Iter 22 extends the same pattern to three more
``Settings`` fields:

  4. ``llm_default_temperature`` — sampled by the Tool
     when the caller does not pass ``temperature=`` to
     ``invoke()``.
  5. ``llm_default_max_tokens`` — sampled when the
     caller does not pass ``max_tokens=`` to
     ``invoke()``.
  6. ``llm_max_cost_usd_per_request`` — the hard cap
     enforced after every successful completion. Calls
     whose ``response.cost_usd`` exceeds the cap are
     rejected with ``Err(ToolError("cost_cap_exceeded"))``.

The tests do NOT exercise the real LiteLLM client
(no imports of litellm in the test scope). They
just verify the attribute values after construction.
"""

from __future__ import annotations

import asyncio

from kntgraph.infra.config import fresh_settings


class TestLiteLLMToolReadsSettings:
    def test_default_model_from_settings(self, monkeypatch):
        """
        ``LiteLLMTool()`` (no args) reads the model
        from Settings. With the default Settings,
        ``llm_default_model="gpt-4o-mini"`` so the
        tool's ``_default_model`` matches.
        """
        # ``litellm`` is imported lazily by ``tool.invoke()``
        # and the import itself reads ``<cwd>/.env`` (via
        # ``litellm``'s own bootstrap). When pytest runs
        # from ``kntgraph.agents/`` with a ``.env`` file that
        # sets ``KNT_LLM_DEFAULT_MODEL``, that value leaks
        # into ``os.environ`` after any prior test invoked
        # the tool. ``monkeypatch.delenv(..., raising=False)``
        # ensures the env var is absent for this test
        # so ``Settings()`` returns the framework default.
        monkeypatch.delenv("KNT_LLM_DEFAULT_MODEL", raising=False)
        # Reload to make sure no env override is in
        # effect from another test.
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool

        # Construct with explicit transport so we
        # don't trigger the lazy adapter import.
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._default_model == "gpt-4o-mini"
        fresh_settings.cache_clear()

    def test_default_timeout_from_settings(self, monkeypatch):
        """The tool's ``_timeout_s`` matches
        ``settings.llm_timeout`` (default 30.0)."""
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._timeout_s == 30.0
        fresh_settings.cache_clear()

    def test_env_override_changes_default_model(self, monkeypatch):
        """
        When ``KNT_LLM_DEFAULT_MODEL`` is set, the
        tool's default changes to match. This is
        the operator's main tuning surface.
        """
        monkeypatch.setenv("KNT_LLM_DEFAULT_MODEL", "claude-3-haiku-20240307")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._default_model == "claude-3-haiku-20240307"
        fresh_settings.cache_clear()

    def test_env_override_changes_timeout(self, monkeypatch):
        """
        When ``KNT_LLM_TIMEOUT`` is set, the tool's
        ``_timeout_s`` matches.
        """
        monkeypatch.setenv("KNT_LLM_TIMEOUT", "60.0")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._timeout_s == 60.0
        fresh_settings.cache_clear()


class TestLiteLLMToolExplicitOverrides:
    def test_explicit_default_model_wins_over_settings(self, monkeypatch):
        """
        Passing ``default_model=`` to the constructor
        must still win over Settings. The tool's
        default is "Settings unless told otherwise".
        """
        monkeypatch.setenv("KNT_LLM_DEFAULT_MODEL", "claude-3-haiku-20240307")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            default_model="gpt-4",
            transport=FakeLLMTransport(),
        )
        # Explicit arg wins over Settings.
        assert tool._default_model == "gpt-4"
        fresh_settings.cache_clear()

    def test_explicit_timeout_wins_over_settings(self, monkeypatch):
        monkeypatch.setenv("KNT_LLM_TIMEOUT", "60.0")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            timeout_s=120.0,
            transport=FakeLLMTransport(),
        )
        assert tool._timeout_s == 120.0
        fresh_settings.cache_clear()

    def test_explicit_default_no_setting(self):
        """When the caller passes ``default_model=`` and
        no env override is set, the explicit arg wins."""
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            default_model="custom-model",
            transport=FakeLLMTransport(),
        )
        assert tool._default_model == "custom-model"
        fresh_settings.cache_clear()


class TestLiteLLMToolTemperatureAndTokens:
    """Iter 22: ``temperature`` and ``max_tokens`` are
    read from ``Settings`` when the caller does not pass
    them to ``invoke()``. Stored on the tool as
    ``_default_temperature`` and ``_default_max_tokens``.
    """

    def test_default_temperature_from_settings(self, monkeypatch):
        """``LiteLLMTool()`` (no args) reads the
        sampling temperature from Settings. The default
        ``llm_default_temperature=0.7`` matches the
        Tool's ``_default_temperature``."""
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._default_temperature == 0.7
        fresh_settings.cache_clear()

    def test_default_max_tokens_from_settings(self, monkeypatch):
        """The tool's ``_default_max_tokens`` matches
        ``settings.llm_default_max_tokens``
        (default 1024)."""
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._default_max_tokens == 1024
        fresh_settings.cache_clear()

    def test_env_override_changes_temperature(self, monkeypatch):
        """When ``KNT_LLM_DEFAULT_TEMPERATURE`` is set,
        the tool's default temperature changes to match.
        """
        monkeypatch.setenv("KNT_LLM_DEFAULT_TEMPERATURE", "0.0")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._default_temperature == 0.0
        fresh_settings.cache_clear()

    def test_env_override_changes_max_tokens(self, monkeypatch):
        """When ``KNT_LLM_DEFAULT_MAX_TOKENS`` is set,
        the tool's default changes to match.
        """
        monkeypatch.setenv("KNT_LLM_DEFAULT_MAX_TOKENS", "4096")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._default_max_tokens == 4096
        fresh_settings.cache_clear()

    def test_explicit_temperature_wins_over_settings(self, monkeypatch):
        """Passing ``temperature=`` to the constructor
        must still win over Settings.
        """
        monkeypatch.setenv("KNT_LLM_DEFAULT_TEMPERATURE", "0.0")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            temperature=1.5,
            transport=FakeLLMTransport(),
        )
        assert tool._default_temperature == 1.5
        fresh_settings.cache_clear()

    def test_explicit_max_tokens_wins_over_settings(self, monkeypatch):
        """Passing ``max_tokens=`` to the constructor
        must still win over Settings.
        """
        monkeypatch.setenv("KNT_LLM_DEFAULT_MAX_TOKENS", "4096")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            max_tokens=512,
            transport=FakeLLMTransport(),
        )
        assert tool._default_max_tokens == 512
        fresh_settings.cache_clear()

    def test_invoke_uses_default_temperature_when_not_given(
        self,
    ):
        """When ``invoke()`` is called without
        ``temperature=``, the call forwarded to the
        transport uses the tool's
        ``_default_temperature`` (from Settings).
        """
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(
            temperature=0.42,
            transport=transport,
        )

        asyncio.run(
            tool.invoke(
                idempotency_key="k",
                system="s",
                user="u",
            )
        )
        assert transport.calls[0]["temperature"] == 0.42
        fresh_settings.cache_clear()

    def test_invoke_uses_default_max_tokens_when_not_given(
        self,
    ):
        """When ``invoke()`` is called without
        ``max_tokens=``, the call forwarded to the
        transport uses the tool's
        ``_default_max_tokens`` (from Settings)."""
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(
            max_tokens=256,
            transport=transport,
        )

        asyncio.run(
            tool.invoke(
                idempotency_key="k",
                system="s",
                user="u",
            )
        )
        assert transport.calls[0]["max_tokens"] == 256
        fresh_settings.cache_clear()

    def test_invoke_explicit_temperature_overrides_default(
        self,
    ):
        """When ``invoke(temperature=...)`` is passed,
        that value wins over the tool's
        ``_default_temperature``."""
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(
            temperature=0.7,
            transport=transport,
        )

        asyncio.run(
            tool.invoke(
                idempotency_key="k",
                system="s",
                user="u",
                temperature=0.0,
            )
        )
        assert transport.calls[0]["temperature"] == 0.0
        fresh_settings.cache_clear()


class TestLiteLLMToolCostCap:
    """Iter 22: ``llm_max_cost_usd_per_request`` is a
    hard cap enforced after every successful completion.
    Calls whose ``response.cost_usd`` exceeds the cap
    are rejected with ``Err(ToolError("cost_cap_exceeded"))``.
    """

    def test_default_cost_cap_from_settings(self, monkeypatch):
        """The tool's ``_max_cost_usd_per_request``
        matches ``settings.llm_max_cost_usd_per_request``
        (default 1.0)."""
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._max_cost_usd_per_request == 1.0
        fresh_settings.cache_clear()

    def test_env_override_changes_cost_cap(self, monkeypatch):
        """When ``KNT_LLM_MAX_COST_USD_PER_REQUEST``
        is set, the tool's cap changes to match.
        """
        monkeypatch.setenv("KNT_LLM_MAX_COST_USD_PER_REQUEST", "0.5")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            transport=FakeLLMTransport(),
        )
        assert tool._max_cost_usd_per_request == 0.5
        fresh_settings.cache_clear()

    def test_explicit_cost_cap_wins_over_settings(self, monkeypatch):
        """Passing ``max_cost_usd_per_request=`` to the
        constructor must still win over Settings.
        """
        monkeypatch.setenv("KNT_LLM_MAX_COST_USD_PER_REQUEST", "0.5")
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            max_cost_usd_per_request=0.1,
            transport=FakeLLMTransport(),
        )
        assert tool._max_cost_usd_per_request == 0.1
        fresh_settings.cache_clear()

    def test_cost_cap_zero_disables_cap(self):
        """Setting ``max_cost_usd_per_request=0`` must
        disable the cap (no call is rejected for cost).
        The sentinel value 0 means "no cap", per
        ``LLMSettingsMixin`` docstring.
        """
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        tool = LiteLLMTool(
            max_cost_usd_per_request=0.0,
            transport=FakeLLMTransport(),
        )
        assert tool._max_cost_usd_per_request == 0.0
        fresh_settings.cache_clear()


class TestLiteLLMToolCostCapEnforcement:
    """Iter 22: post-call cost-cap enforcement.

    When a successful response reports
    ``cost_usd > _max_cost_usd_per_request`` (and the
    cap is non-zero), ``invoke()`` returns
    ``Err(ToolError("cost_cap_exceeded"))`` instead of
    ``Ok(response)``. When ``cost_usd`` is ``None`` (no
    pricing data, e.g. local models), the cap is
    skipped — we cannot reject what we cannot measure.
    """

    def test_call_within_cap_succeeds(self):
        """A call whose ``cost_usd`` is below the cap
        returns ``Ok(response)``."""
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        transport = FakeLLMTransport()
        transport.queue_response(text="ok", cost_usd=0.001)
        tool = LiteLLMTool(
            max_cost_usd_per_request=0.5,
            transport=transport,
        )

        result = asyncio.run(
            tool.invoke(
                idempotency_key="k",
                system="s",
                user="u",
            )
        )
        assert result.is_ok()
        fresh_settings.cache_clear()

    def test_call_above_cap_rejected(self):
        """A call whose ``cost_usd`` exceeds the cap
        returns ``Err(ToolError("cost_cap_exceeded"))``.
        The response is NOT propagated to the caller.
        """
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        transport = FakeLLMTransport()
        transport.queue_response(text="ok", cost_usd=2.0)
        tool = LiteLLMTool(
            max_cost_usd_per_request=0.5,
            transport=transport,
        )

        result = asyncio.run(
            tool.invoke(
                idempotency_key="k",
                system="s",
                user="u",
            )
        )
        assert result.is_err()
        err = result.err_value()
        assert "cost_cap_exceeded" in str(err)
        fresh_settings.cache_clear()

    def test_call_with_unknown_cost_skips_cap(self):
        """When ``cost_usd`` is ``None`` (provider does
        not report cost), the cap is skipped — we cannot
        reject what we cannot measure. This is the
        behaviour for local models (Ollama).
        """
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        transport = FakeLLMTransport()
        # queue_response with cost_usd=None simulates
        # a provider that does not report cost.
        transport.queue_response(text="ok", cost_usd=None)
        tool = LiteLLMTool(
            max_cost_usd_per_request=0.5,
            transport=transport,
        )

        result = asyncio.run(
            tool.invoke(
                idempotency_key="k",
                system="s",
                user="u",
            )
        )
        assert result.is_ok()
        fresh_settings.cache_clear()

    def test_cap_disabled_allows_expensive_calls(self):
        """When ``max_cost_usd_per_request=0`` (cap
        disabled), no call is rejected for cost.
        """
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        transport = FakeLLMTransport()
        transport.queue_response(text="ok", cost_usd=100.0)
        tool = LiteLLMTool(
            max_cost_usd_per_request=0.0,
            transport=transport,
        )

        result = asyncio.run(
            tool.invoke(
                idempotency_key="k",
                system="s",
                user="u",
            )
        )
        assert result.is_ok()
        fresh_settings.cache_clear()

    def test_cost_cap_message_includes_threshold(self):
        """The error message includes the cap threshold
        so operators can debug without consulting
        Settings.
        """
        fresh_settings.cache_clear()
        from kntgraph.agents.tools.llm import LiteLLMTool
        from .._fake_transport import (
            FakeLLMTransport,
        )

        transport = FakeLLMTransport()
        transport.queue_response(text="ok", cost_usd=2.0)
        tool = LiteLLMTool(
            max_cost_usd_per_request=0.5,
            transport=transport,
        )

        result = asyncio.run(
            tool.invoke(
                idempotency_key="k",
                system="s",
                user="u",
            )
        )
        assert "0.5" in str(result.err_value())
        fresh_settings.cache_clear()
