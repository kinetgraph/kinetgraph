# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
The framework clock (ADR-085 E).

``core.clock`` is the single authority for "now" in the
framework. Its rule:

| Use | Clock | Injected | Persisted |
|-----|-------|----------|-----------|
| Absolute instants, timestamps, guards, deadlines | ``utcnow`` | Yes | Yes |
| Pure duration on a hot path | ``monotonic`` | No | No |

ADR-085 found **two** places that bypassed it:

- the TTL sweeper called ``datetime.now(tz=UTC)`` directly
  instead of ``core.clock.utcnow()``;
- the ``ToolRouter``'s TTL arithmetic had no clock at all.

These tests pin the contract the ADR-085 correction establishes:

1. ``utcnow()`` is timezone-aware UTC and is the **single**
   definition (no module re-implements it);
2. ``injectable_clock`` returns the injected clock or
   ``utcnow``;
3. a module that needs "now" must source it from
   ``core.clock`` — no direct ``datetime.now`` calls.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kntgraph.core.clock import Clock, injectable_clock, monotonic, utcnow

SRC = Path(__file__).resolve().parents[3] / "src" / "kntgraph"


def _is_datetime_now_call(node: ast.AST) -> bool:
    """True when ``node`` is a ``datetime.now(...)`` call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in ("now", "utcnow"):
        return False
    owner = func.value
    return isinstance(owner, ast.Name) and owner.id == "datetime"


class TestUtcnow:
    def test_returns_timezone_aware_utc(self) -> None:
        now = utcnow()
        assert now.tzinfo is not None
        assert now.utcoffset() == UTC.utcoffset(None)

    def test_is_close_to_wall_clock(self) -> None:
        before = datetime.now(tz=UTC)
        now = utcnow()
        after = datetime.now(tz=UTC)
        assert before <= now <= after

    def test_is_the_single_definition(self) -> None:
        """No module may re-implement ``utcnow``.

        The framework had two definitions before ``core.clock``
        existed; a third would reintroduce the drift. The
        re-export in ``core/event/validators.py`` is allowed
        (it delegates), but a fresh ``datetime.now(tz=UTC)``
        body is not.
        """
        offenders: list[str] = []
        for path in SRC.rglob("*.py"):
            if path.name == "clock.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "utcnow":
                    # A re-export delegates to core.clock; a
                    # re-implementation does not.
                    body = ast.dump(node)
                    if "core.clock" not in body and "_utcnow" not in body:
                        offenders.append(str(path.relative_to(SRC)))
        assert offenders == [], (
            f"these modules re-implement utcnow(): {offenders} — "
            f"import it from kntgraph.core.clock instead"
        )

    def test_no_module_calls_datetime_now_as_a_clock_fallback(self) -> None:
        """A module that resolves a **fallback** for an
        injectable clock must use ``core.clock.utcnow()``.

        ``x or datetime.now(tz=UTC)`` is the pattern: the module
        accepts an injected clock and falls back to the
        framework's. Calling ``datetime.now`` directly there
        bypasses the authority — the fallback is not injectable
        and not the canonical ``utcnow``.

        The TTL sweeper did exactly this (ADR-085 E), and so did
        the observability helpers.
        """
        offenders: list[str] = []
        for path in SRC.rglob("*.py"):
            if path.name == "clock.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # ``<expr> or datetime.now(...)``
                if not isinstance(node, ast.BoolOp):
                    continue
                if not isinstance(node.op, ast.Or):
                    continue
                for value in node.values:
                    if _is_datetime_now_call(value):
                        offenders.append(str(path.relative_to(SRC)))
                        break
        assert offenders == [], (
            f"these modules fall back to datetime.now() instead of "
            f"core.clock.utcnow(): {offenders} — the fallback must be "
            f"the framework's canonical clock (ADR-085 E)"
        )


class TestInjectableClock:
    def test_returns_the_injected_clock(self) -> None:
        fixed = datetime(2026, 10, 24, tzinfo=UTC)
        clock = injectable_clock(lambda: fixed)
        assert clock() == fixed

    def test_falls_back_to_utcnow(self) -> None:
        clock = injectable_clock(None)
        assert clock() is not None
        assert clock().tzinfo is not None

    def test_a_clock_is_callable(self) -> None:
        clock: Clock = injectable_clock(None)
        assert callable(clock)


class TestMonotonic:
    def test_is_monotonic(self) -> None:
        """``monotonic`` never goes backwards — the property the
        circuit breaker's ``recovery_timeout`` relies on."""
        values = [monotonic() for _ in range(100)]
        assert values == sorted(values)

    def test_is_not_a_datetime(self) -> None:
        """``monotonic`` measures a duration; it is never
        persisted, so it must not be a wall-clock instant."""
        assert not isinstance(monotonic(), datetime)


class TestTestingClock:
    def test_fixed_now_is_injectable(self) -> None:
        """``testing.clock.fixed_now`` is the deterministic
        counterpart the TTL sweeper's tests use."""
        from kntgraph.testing.clock import fixed_now

        assert fixed_now() == fixed_now()

    @pytest.mark.parametrize("value", [None, 0, ""])
    def test_injectable_clock_handles_falsy(self, value: object) -> None:
        """A falsy injected clock must fall back to ``utcnow``,
        not crash — ``injectable_clock`` is called from
        ``__init__``s that may receive ``None``."""
        clock = injectable_clock(value)  # type: ignore[arg-type]
        assert clock() is not None
