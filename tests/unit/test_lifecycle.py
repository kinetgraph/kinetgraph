# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the lifecycle module (v2.0).

Two axes:
  - OperationalPhase: framework-defined; controls runtime mode.
  - DomainPhase: application-defined; controls business step.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kntgraph.core.lifecycle import (
    DomainPhase,
    TERMINAL_OPERATIONAL,
    is_terminal_operational,
)


class TestOperationalPhases:
    def test_terminal_phases(self):
        assert is_terminal_operational("terminated")
        assert not is_terminal_operational("spawned")
        assert not is_terminal_operational("running")
        assert not is_terminal_operational("idle")
        assert not is_terminal_operational("blocked")
        assert not is_terminal_operational("checkpointed")

    def test_terminal_set(self):
        assert "terminated" in TERMINAL_OPERATIONAL
        assert "running" not in TERMINAL_OPERATIONAL


class TestDomainPhase:
    def test_construct(self):
        ts = datetime.now(timezone.utc)
        dp = DomainPhase(phase="validated", updated_at=ts)
        assert dp.phase == "validated"
        assert dp.updated_at == ts
        assert dp.reason is None

    def test_with_reason(self):
        ts = datetime.now(timezone.utc)
        dp = DomainPhase(
            phase="rejected",
            updated_at=ts,
            reason="missing CNPJ",
        )
        assert dp.reason == "missing CNPJ"

    def test_str_returns_phase(self):
        ts = datetime.now(timezone.utc)
        dp = DomainPhase(phase="paid", updated_at=ts)
        assert str(dp) == "paid"

    def test_immutable(self):
        ts = datetime.now(timezone.utc)
        dp = DomainPhase(phase="x", updated_at=ts)
        try:
            dp.phase = "y"  # type: ignore[misc]
            assert False, "should have raised"
        except Exception:
            pass
