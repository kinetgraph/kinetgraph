# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Reactive sub-config (mixin).

Cadence knobs for the background loops that observe the
EventLog. These are the framework's de-facto latency/traffic
knobs (ADR-068): every poll interval below bounds both the
worst-case reaction latency and the idle Redis round-trip
rate. Operators tune them per deployment; the defaults are
conservative (traffic-minimising) starting points.

  - ``reactive_poll_interval``     — ReactiveDispatcher tick
    cadence (per-agent checkpoint read + EventLog read).
  - ``reactive_rediscovery_seconds`` — how often the
    dispatcher re-runs ``list_agents()`` to pick up
    brand-new agents.
  - ``warmer_pump_interval``       — CacheWarmer bus pump
    cadence.
  - ``fallback_poll_interval``     — consumer-side poll used
    when the push channel (``EventLog.subscribe``, ADR-068
    Phase 1+) is silent. Bounds the worst-case latency of a
    lost notification; must be strictly positive.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from kntgraph.infra.config._base import BaseSettings


class ReactiveSettingsMixin(BaseSettings):
    """Cadence knobs for the EventLog observer loops (ADR-068 §3.8)."""

    reactive_poll_interval: float = Field(default=0.25)
    reactive_rediscovery_seconds: float = Field(default=5.0)
    warmer_pump_interval: float = Field(default=0.25)
    fallback_poll_interval: float = Field(default=5.0)

    @model_validator(mode="after")
    def _validate_positive_cadences(self) -> "ReactiveSettingsMixin":
        """
        A zero or negative cadence would either busy-spin the
        loop (poll intervals) or make the fallback poll fire
        so rarely that a lost notification stalls a consumer
        indefinitely. The push model (ADR-068) is a latency
        optimisation; the fallback poll is the correctness
        net — it must always be reachable.
        """
        knobs: tuple[tuple[str, float], ...] = (
            ("reactive_poll_interval", self.reactive_poll_interval),
            ("reactive_rediscovery_seconds", self.reactive_rediscovery_seconds),
            ("warmer_pump_interval", self.warmer_pump_interval),
            ("fallback_poll_interval", self.fallback_poll_interval),
        )
        for name, value in knobs:
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        return self


__all__ = ["ReactiveSettingsMixin"]
