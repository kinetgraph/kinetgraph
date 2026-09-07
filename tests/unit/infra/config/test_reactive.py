# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the Reactive Settings mixin (ADR-068 §3.8).

The four cadence knobs own the idle-traffic/latency
trade-off of the EventLog observer loops. The tests pin the
defaults (they are the shipped configuration), the env-var
override path (``KNT_`` flat namespace), and the positive
invariant (a zero/negative cadence busy-spins the loop or
stalls a push consumer forever).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kntgraph.infra.config import Settings, fresh_settings


class TestReactiveDefaults:
    def test_reactive_poll_interval_default(self):
        s = Settings()
        assert s.reactive_poll_interval == 0.25

    def test_reactive_rediscovery_default(self):
        s = Settings()
        assert s.reactive_rediscovery_seconds == 5.0

    def test_warmer_pump_interval_default(self):
        s = Settings()
        assert s.warmer_pump_interval == 0.25

    def test_fallback_poll_interval_default(self):
        s = Settings()
        assert s.fallback_poll_interval == 5.0


class TestReactiveEnvOverride:
    def test_poll_interval_override(self, monkeypatch):
        monkeypatch.setenv("KNT_REACTIVE_POLL_INTERVAL", "2.0")
        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.reactive_poll_interval == 2.0
        fresh_settings.cache_clear()

    def test_rediscovery_override(self, monkeypatch):
        monkeypatch.setenv("KNT_REACTIVE_REDISCOVERY_SECONDS", "10.0")
        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.reactive_rediscovery_seconds == 10.0
        fresh_settings.cache_clear()

    def test_warmer_override(self, monkeypatch):
        monkeypatch.setenv("KNT_WARMER_PUMP_INTERVAL", "1.5")
        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.warmer_pump_interval == 1.5
        fresh_settings.cache_clear()

    def test_fallback_override(self, monkeypatch):
        monkeypatch.setenv("KNT_FALLBACK_POLL_INTERVAL", "30.0")
        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.fallback_poll_interval == 30.0
        fresh_settings.cache_clear()


class TestReactiveValidation:
    @pytest.mark.parametrize(
        ("field", "bad"),
        [
            ("reactive_poll_interval", "0"),
            ("reactive_poll_interval", "-1.0"),
            ("reactive_rediscovery_seconds", "0"),
            ("reactive_rediscovery_seconds", "-0.5"),
            ("warmer_pump_interval", "0"),
            ("warmer_pump_interval", "-2.0"),
            ("fallback_poll_interval", "0"),
            ("fallback_poll_interval", "-5.0"),
        ],
    )
    def test_non_positive_cadence_rejected(self, monkeypatch, field, bad):
        """A zero/negative cadence would busy-spin a poll
        loop or make a lost push notification stall a
        consumer indefinitely (the fallback poll is the
        correctness net of the push model — ADR-068 §3.1)."""
        monkeypatch.setenv(f"KNT_{field.upper()}", bad)
        fresh_settings.cache_clear()
        with pytest.raises(ValidationError):
            Settings()
        fresh_settings.cache_clear()

    def test_fallback_must_exceed_poll_interval_is_not_enforced(self):
        """Documented non-constraint: the fallback poll may
        legitimately be shorter than the reactive poll (a
        consumer that wakes on push polls rarely; a poll-only
        consumer may want both tight). The validator only
        enforces positivity."""
        s = Settings()
        assert s.fallback_poll_interval > 0
        assert s.reactive_poll_interval > 0
