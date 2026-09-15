# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
testing.clock -- deterministic clock helpers for tests.

Production code MUST inject the framework's canonical
``injectable_clock(now)`` (see ``core.clock``). Tests follow
the same pattern: they construct a ``WorldSystem`` with
``now=lambda: fixed_now()`` so saga deadlines / FSM
transitions / etc. are evaluated against a frozen instant
rather than wall-clock.

Before this module each test file redeclared the same
``FIXED_NOW = datetime(...)`` constant (a 3-way copy). The
single source of truth lives here so the framework's
canonical date is consistent across the test suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["fixed_now"]


# A frozen UTC instant. The actual value is irrelevant —
# only its stability across a test run matters. Tests that
# need to assert on a relative timestamp (e.g. "expired 6
# minutes ago") compute ``fixed_now() - timedelta(...)``.
_FIXED_NOW: datetime = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def fixed_now() -> datetime:
    """Return the framework's canonical frozen ``now`` for
    tests.

    The clock-injection convention is::

        system = FSMSystem(config, now=lambda: fixed_now())
        out = run_system(system, world, correlation=ctx)

    The lambda ensures the system evaluates ``self._now()``
    against the test's frozen clock on every call (so a
    saga deadline check, say, is reproducible), while the
    constant itself is stable for the lifetime of the test.

    Production code MUST NOT call this — it is test-only.
    The framework's wall-clock is ``core.clock.utcnow``.
    """
    return _FIXED_NOW
