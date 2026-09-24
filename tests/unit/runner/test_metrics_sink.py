# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Behaviour tests for the ``MetricsSink`` Protocol and the
``NullMetricsSink`` default.

These tests do NOT need Redis, a dispatcher tick loop, or a
metrics backend installed. They pin the Protocol contract
that any custom backend must honour and the no-op
behaviour that keeps the dispatcher's hot path unchanged
when no backend is wired.

The dispatcher integration (sink gets called at the right
moments) is pinned in
:mod:`tests.unit.runner.test_metrics_integration`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kntgraph.runner import MetricsSink, NullMetricsSink


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


def test_null_metrics_sink_methods_return_none() -> None:
    """Every ``NullMetricsSink`` method returns ``None`` and
    accepts arbitrary integer / no args without raising.

    This is the contract that lets the dispatcher call the
    sink unconditionally without checking ``metrics_sink is
    None`` at every call site.
    """
    sink = NullMetricsSink()
    assert sink.record_in_flight(0) is None
    assert sink.record_in_flight(42) is None
    assert sink.record_stale(0) is None
    assert sink.record_stale(7) is None
    assert sink.record_stuck_in_queue(0) is None
    assert sink.record_dead_lettered(3) is None
    assert sink.incr_compensation_started() is None


def test_metrics_sink_is_runtime_checkable() -> None:
    """``MetricsSink`` is ``runtime_checkable`` so callers can
    assert against it without importing the concrete class.

    Both ``NullMetricsSink`` (the framework default) and a
    custom sink implementing the five methods satisfy the
    Protocol.
    """
    assert isinstance(NullMetricsSink(), MetricsSink)

    @dataclass
    class _Custom:
        in_flight_calls: list[int] = field(default_factory=list)
        stale_calls: list[int] = field(default_factory=list)
        stuck_calls: list[int] = field(default_factory=list)
        dlq_calls: list[int] = field(default_factory=list)
        compensation_calls: int = 0

        def record_in_flight(self, count: int) -> None:
            self.in_flight_calls.append(count)

        def record_stale(self, count: int) -> None:
            self.stale_calls.append(count)

        def record_stuck_in_queue(self, count: int) -> None:
            self.stuck_calls.append(count)

        def record_dead_lettered(self, count: int) -> None:
            self.dlq_calls.append(count)

        def incr_compensation_started(self) -> None:
            self.compensation_calls += 1

    custom = _Custom()
    assert isinstance(custom, MetricsSink)
    custom.record_in_flight(5)
    custom.record_stale(2)
    custom.record_stuck_in_queue(1)
    custom.record_dead_lettered(3)
    custom.incr_compensation_started()
    custom.incr_compensation_started()
    assert custom.in_flight_calls == [5]
    assert custom.stale_calls == [2]
    assert custom.stuck_calls == [1]
    assert custom.dlq_calls == [3]
    assert custom.compensation_calls == 2


def test_metrics_sink_protocol_exposes_five_methods() -> None:
    """The Protocol surface is exactly five methods.

    Pinning this prevents accidental addition of methods
    that custom sinks would silently fail to implement.
    """
    expected = {
        "record_in_flight",
        "record_stale",
        "record_stuck_in_queue",
        "record_dead_lettered",
        "incr_compensation_started",
    }
    assert set(dir(MetricsSink)) >= expected
    # All five must be callable.
    sink = NullMetricsSink()
    for name in expected:
        method = getattr(sink, name)
        assert callable(method)


# ---------------------------------------------------------------------------
# Re-export surface
# ---------------------------------------------------------------------------


def test_runner_public_surface_includes_metrics_sink() -> None:
    """``kntgraph.runner`` re-exports ``MetricsSink`` and
    ``NullMetricsSink`` so applications can wire them
    without reaching into the private ``_metrics`` module.
    """
    import kntgraph.runner as runner

    assert runner.MetricsSink is MetricsSink
    assert runner.NullMetricsSink is NullMetricsSink
    assert "MetricsSink" in runner.__all__
    assert "NullMetricsSink" in runner.__all__


# ---------------------------------------------------------------------------
# Default wiring on the dispatcher constructor
# ---------------------------------------------------------------------------


def test_dispatcher_default_sink_is_null_metrics_sink() -> None:
    """A dispatcher constructed without ``metrics_sink=``
    installs :class:`NullMetricsSink` so the hot path stays
    unchanged.
    """
    from kntgraph.runner.reactive import ReactiveDispatcher

    # ``world_store`` is required by the constructor; the
    # test only inspects the stored sink, never the world
    # store, so a stub returning an empty World is enough.
    dispatcher = ReactiveDispatcher(
        log=_StubLog(),
        world_store=_StubWorldStore(),
    )
    assert isinstance(dispatcher._metrics_sink, NullMetricsSink)


def test_dispatcher_uses_passed_sink() -> None:
    """When ``metrics_sink=`` is provided, the dispatcher
    stores it verbatim (no copy, no wrap).
    """
    from kntgraph.runner.reactive import ReactiveDispatcher

    @dataclass
    class _Sink:
        marker: str = "custom"

        def record_in_flight(self, count: int) -> None:
            pass

        def record_stale(self, count: int) -> None:
            pass

        def record_stuck_in_queue(self, count: int) -> None:
            pass

        def record_dead_lettered(self, count: int) -> None:
            pass

        def incr_compensation_started(self) -> None:
            pass

    sink = _Sink()
    dispatcher = ReactiveDispatcher(
        log=_StubLog(),
        world_store=_StubWorldStore(),
        metrics_sink=sink,
    )
    assert dispatcher._metrics_sink is sink


# ---------------------------------------------------------------------------
# Prometheus sink import path (gated by the optional extra)
# ---------------------------------------------------------------------------


def test_prometheus_sink_importable_via_module_path() -> None:
    """``kntgraph.runner.metrics.prometheus`` is importable
    in isolation when ``prometheus_client`` is installed.

    The module is guarded: ``prometheus_client`` is imported
    lazily inside ``PrometheusMetricsSink.__init__``. The
    module itself only declares type-level imports and
    constant metric names -- it can be imported without the
    extra installed.
    """
    pytest.importorskip("prometheus_client")
    from kntgraph.runner.metrics.prometheus import (
        PrometheusMetricsSink,
    )

    assert issubclass(PrometheusMetricsSink, object)


# ---------------------------------------------------------------------------
# Stubs used by the constructor tests
# ---------------------------------------------------------------------------


class _StubLog:
    """Bare-minimum ``EventLog`` stand-in.

    The constructor only assigns ``self._log = log`` and
    does not call any method; the test never exercises the
    tick loop, so an empty class is enough.
    """


class _StubWorldStore:
    """Bare-minimum ``IncrementalWorldStore`` stand-in.

    The constructor assigns ``self._world_store = store``.
    The constructor tests never read or call methods on the
    store; this stub exists only to satisfy the
    "either ``world_store`` or ``redis``" guard.
    """
