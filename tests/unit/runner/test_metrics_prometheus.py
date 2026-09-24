# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Behaviour tests for ``PrometheusMetricsSink``.

These tests require the optional ``prometheus_client``
dependency (installed via ``kntgraph[metrics]``). They are
skipped when the package is not installed -- the framework
stays importable without the extra.

The tests use a private :class:`CollectorRegistry` per
case so counter increments do not leak between tests (the
singleton ``prometheus_client.REGISTRY`` would accumulate
across cases and break the "starts at zero" assertion).
"""

from __future__ import annotations

import pytest

pytest.importorskip("prometheus_client")

from prometheus_client import CollectorRegistry  # noqa: E402

from kntgraph.runner.metrics.prometheus import (  # noqa: E402
    PrometheusMetricsSink,
)


def _build_sink() -> tuple[PrometheusMetricsSink, CollectorRegistry]:
    """Build a sink with an isolated registry.

    Returns the sink and the registry so the test can read
    the recorded metric values directly via
    ``registry.get_sample_value(...)``.
    """
    registry = CollectorRegistry()
    sink = PrometheusMetricsSink(registry=registry)
    return sink, registry


def test_gauge_values_are_recorded() -> None:
    """Each ``record_*`` call sets the corresponding gauge
    to the supplied integer. The gauge value is readable
    via ``registry.get_sample_value(...)``.
    """
    sink, registry = _build_sink()
    sink.record_in_flight(5)
    sink.record_stale(2)
    sink.record_stuck_in_queue(1)
    sink.record_dead_lettered(3)

    assert registry.get_sample_value("knt_reactive_in_flight_tasks") == 5.0
    assert registry.get_sample_value("knt_reactive_stale_tasks") == 2.0
    assert registry.get_sample_value("knt_reactive_stuck_in_queue") == 1.0
    assert registry.get_sample_value("knt_reactive_dead_lettered_tasks") == 3.0


def test_counter_increments_monotonically() -> None:
    """The compensation counter is monotonic. Three
    increments yield a sample value of 3.0; a fourth
    increments to 4.0. There is no public ``decr`` path.

    A freshly-constructed :class:`Counter` starts at 0
    (``prometheus_client`` returns ``0.0`` from
    ``get_sample_value`` even before any increment); the
    test pins the monotonic-up behaviour, not the
    ``None``-vs-``0.0`` quirk.
    """
    sink, registry = _build_sink()
    counter_name = "knt_saga_compensations_started_total"
    assert registry.get_sample_value(counter_name) == 0.0
    sink.incr_compensation_started()
    sink.incr_compensation_started()
    sink.incr_compensation_started()
    assert registry.get_sample_value(counter_name) == 3.0
    sink.incr_compensation_started()
    assert registry.get_sample_value(counter_name) == 4.0


def test_sink_is_metrics_sink_subclass() -> None:
    """``PrometheusMetricsSink`` satisfies the framework's
    :class:`MetricsSink` Protocol structurally (it can be
    passed as ``metrics_sink=`` to ``ReactiveDispatcher``).
    """
    from kntgraph.runner import MetricsSink

    sink, _ = _build_sink()
    assert isinstance(sink, MetricsSink)


def test_two_sinks_with_private_registries_are_independent() -> None:
    """Two sinks built with separate ``CollectorRegistry``
    instances do not share state. The "starts at zero"
    property of a fresh registry is preserved per sink.
    """
    sink_a, registry_a = _build_sink()
    sink_b, registry_b = _build_sink()

    sink_a.record_in_flight(10)
    sink_b.record_in_flight(20)
    sink_a.incr_compensation_started()
    sink_a.incr_compensation_started()

    assert registry_a.get_sample_value("knt_reactive_in_flight_tasks") == 10.0
    assert registry_b.get_sample_value("knt_reactive_in_flight_tasks") == 20.0
    assert registry_a.get_sample_value("knt_saga_compensations_started_total") == 2.0
    # ``prometheus_client`` reports freshly-created counters
    # at ``0.0`` (not ``None``), so the second registry shows
    # 0.0 -- not None -- when its sink has never incremented.
    assert registry_b.get_sample_value("knt_saga_compensations_started_total") == 0.0
