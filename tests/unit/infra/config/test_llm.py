# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the LLM Settings mixin.

Iter 19 (ADR-019 epílogo + Iter 19 do sharding):
the LLM adapter (``LiteLLMTransportAdapter``) was
hard-coding model defaults (``"gpt-4o-mini"``,
``temperature=0.7``, etc.) instead of reading from
``Settings``. This makes per-deployment tuning
awkward — operators must subclass the Tool.

The fix: a ``LLMSettingsMixin`` in
``kntgraph.infra.config._llm`` that pins all
LLM knobs. ``Settings`` inherits the mixin. The
LLM adapter reads via ``Settings()`` at construction.
"""

from __future__ import annotations


from kntgraph.infra.config import Settings


class TestLLMDefaults:
    def test_default_model(self):
        s = Settings()
        assert s.llm_default_model == "gpt-4o-mini"

    def test_default_temperature(self):
        s = Settings()
        assert s.llm_default_temperature == 0.7

    def test_default_max_tokens(self):
        s = Settings()
        assert s.llm_default_max_tokens == 1024

    def test_timeout_already_set(self):
        """The framework already had `llm_timeout=30.0`
        in the monolithic Settings. Iter 19 keeps the
        field name and default; we just add the
        neighbouring fields."""
        s = Settings()
        assert s.llm_timeout == 30.0

    def test_max_cost_per_request(self):
        s = Settings()
        assert s.llm_max_cost_usd_per_request == 1.0

    def test_max_cost_per_request_is_positive(self):
        s = Settings()
        assert s.llm_max_cost_usd_per_request > 0


class TestLLMEnvOverride:
    def test_model_override(self, monkeypatch):
        monkeypatch.setenv("FMH_LLM_DEFAULT_MODEL", "claude-3-haiku")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.llm_default_model == "claude-3-haiku"
        fresh_settings.cache_clear()

    def test_temperature_override(self, monkeypatch):
        monkeypatch.setenv("FMH_LLM_DEFAULT_TEMPERATURE", "0.0")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.llm_default_temperature == 0.0
        fresh_settings.cache_clear()

    def test_max_tokens_override(self, monkeypatch):
        monkeypatch.setenv("FMH_LLM_DEFAULT_MAX_TOKENS", "4096")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.llm_default_max_tokens == 4096
        fresh_settings.cache_clear()


class TestLLMTemperatureValidation:
    def test_temperature_must_be_in_range(self):
        s = Settings()
        # Pydantic validates ``ge=0, le=2`` (LLM range).
        assert 0 <= s.llm_default_temperature <= 2


class TestLLMMaxTokensValidation:
    def test_max_tokens_must_be_positive(self):
        s = Settings()
        assert s.llm_default_max_tokens > 0
