# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
LLM tier sub-config (mixin).

Holds the LLM knobs that the framework exposes
end-to-end:

  - ``llm_default_model`` — the model tag passed to
    ``LiteLLMTransportAdapter.complete(model=...)``
    when the caller does not specify one.
  - ``llm_default_temperature`` — the default sampling
    temperature (0.0 = deterministic, 2.0 = creative).
  - ``llm_default_max_tokens`` — the default
    ``max_tokens`` cap on a single completion.
  - ``llm_timeout`` — per-call timeout (the underlying
    transport raises on timeout; the Tool translates
    to ``Result[Err(...)]``).
  - ``llm_max_cost_usd_per_request`` — a hard cap on
    the cost the LLM reports back. Calls whose
    ``cost_usd`` exceeds this value are rejected by
    the Tool before being persisted to the EventLog.
    Set to 0 to disable the cap.

Why a mixin (not a free-standing Settings)
-----------------------------------------

The framework already has a single ``Settings``
class (in ``_base.py``) that is the canonical
project config. Sub-configs (Knowledge, LLM,
Embedding) are mixed in via multiple inheritance
so a single ``Settings()`` instance exposes every
knob. This avoids a proliferation of config
singletons (each would need its own
``fresh_settings()`` cache).

Env prefix
----------

The base ``Settings`` pins ``env_prefix="KNT_"``;
the mixin therefore reads from
``KNT_LLM_DEFAULT_MODEL`` etc. Operators set
these in the deployment manifest (Kubernetes
``ConfigMap``, Docker ``--env-file``, etc).
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class LLMSettingsMixin(BaseSettings):
    """LLM model + generation + timeout + cost knobs."""

    # Model selection. ``gpt-4o-mini`` is a sensible
    # default for low-latency MVPs; production
    # deployments override via env.
    llm_default_model: str = Field(default="gpt-4o-mini")
    # Sampling temperature: 0.0 (deterministic) to
    # 2.0 (very creative). 0.7 is the OpenAI default
    # for chat. Bounded so a typo (e.g. 7) is caught
    # at construction, not after a costly batch.
    llm_default_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # ``max_tokens`` cap. 1024 fits a typical
    # non-streaming JSON-schema response; production
    # deployments may want 4096 for long-form.
    llm_default_max_tokens: int = Field(default=1024, gt=0)
    # Per-call timeout. The transport's underlying
    # ``asyncio.to_thread`` is bounded by this; on
    # timeout the Tool returns ``Err(...)`` with a
    # clear message.
    llm_timeout: float = Field(default=30.0, gt=0)
    # Cost guardrail. The Tool rejects completions
    # whose reported ``cost_usd`` exceeds this cap.
    # ``0`` disables the cap (use with care).
    llm_max_cost_usd_per_request: float = Field(default=1.0, ge=0.0)
