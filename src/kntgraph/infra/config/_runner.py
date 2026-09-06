# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
#
"""
Runner sub-config (mixin).

Holds the post-tick loop interval — the smallest unit
of time the framework uses to schedule work.
Also pins the ADR-068 Phase 8 knobs for reactive dispatcher
polls, warmer cadence and fallback intervals.
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class RunnerSettingsMixin(BaseSettings):
    """Post-tick loop interval in seconds.

    ADR-068 Phase 8: all knobs are env‑vars prefixed with ``KNT_``;
    the aggregated ``Settings`` (``KNT_`` prefix) picks them up.
    """

    tick_interval: float = Field(default=1.0)
    # ADR-068 §3.8 / P8 — reactive dispatcher poll cadences.
    # Hardcoded values in the audit were:
    #   - poll_interval = 0.25 s (runner/reactive.py:131)
    #   - rediscovery_interval_seconds = 5 s (runner/reactive.py:137)
    #   - warmer_pump_interval = 0.25 s (memory/cache_warmer.py:182)
    #   - fallback_poll_interval = 5 s (new, §3.1)
    # All are now read from ``KNT_`` env vars (the aggregated
    # ``Settings`` has ``env_prefix="KNT_"``) so operators can
    # tune them without a code redeploy.  Field names map
    # directly (case‑insensitive) to the corresponding env var.
    # The reactive dispatcher reads these three specific names:
    #   reactive_poll_interval
    #   reactive_rediscovery_seconds
    #   fallback_poll_interval
    reactive_poll_interval: float = Field(default=0.25)
    reactive_rediscovery_seconds: float = Field(default=5)
    warmer_pump_interval: float = Field(default=0.25)
    fallback_poll_interval: float = Field(default=5)
