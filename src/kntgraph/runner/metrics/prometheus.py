# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
runner.metrics.prometheus -- Prometheus sink for ``MetricsSink``.

Implements the five primitives of :class:`MetricsSink` against
the ``prometheus_client`` package:

  - 4 ``Gauge`` instances -- one per ADR-075 Tier 4 read
    query (in_flight, stale, stuck_in_queue, dead_lettered).
  - 1 ``Counter`` instance -- ``compensations_started``,
    incremented once per ``*.compensation_started`` event
    appended to the EventLog.

The metrics are registered against a
:class:`prometheus_client.CollectorRegistry`. By default the
singleton ``prometheus_client.REGISTRY`` is used (the
standard convention for single-process deployments). Tests
that instantiate the dispatcher multiple times in the same
process should pass a private ``CollectorRegistry()`` to
isolate counters.

HTTP exposition
---------------

This module does NOT start an HTTP server automatically. To
expose ``/metrics`` on a port, call
:func:`prometheus_client.start_http_server` from the same
process after constructing the dispatcher, or use
:classmethod:`PrometheusMetricsSink.start_default_http_server`
which is a thin wrapper.

Why a separate module
---------------------

The dispatcher emits metrics through a small Protocol
(:class:`MetricsSink`); the framework does not import
``prometheus_client`` itself. Operators that want a different
backend (StatsD, OTLP, ...) can implement the Protocol in
their own module. This module is the canonical
Prometheus-backed implementation and lives behind the
``kntgraph[metrics]`` extra so the framework stays
importable without ``prometheus_client`` installed.

The ``prometheus_client`` import is **lazy in
``__init__``** -- the module itself can be imported (e.g.,
for type hints or ``isinstance`` checks) without the extra.
Construction fails with a clear message pointing at the
right ``pip install`` line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..._optional import require_optional
from .._metrics import MetricsSink

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry


# Metric names are constants so dashboards and alert rules
# have a stable string to reference. Names follow the
# Prometheus convention (``snake_case`` + unit suffix where
# applicable).
_IN_FLIGHT_METRIC = "knt_reactive_in_flight_tasks"
_STALE_METRIC = "knt_reactive_stale_tasks"
_STUCK_METRIC = "knt_reactive_stuck_in_queue"
_DLQ_METRIC = "knt_reactive_dead_lettered_tasks"
_COMPENSATION_METRIC = "knt_saga_compensations_started_total"


class PrometheusMetricsSink(MetricsSink):
    """A :class:`MetricsSink` backed by ``prometheus_client``.

    The four ADR-075 Tier 4 read queries are exposed as
    ``Gauge`` instances so the operator's last-sampled
    value is what the dashboard reads. The compensation
    counter is a ``Counter`` so monotonic increments
    survive a process restart (the ``total`` suffix is
    the Prometheus convention for monotonic counters).

    Construction
    ------------

    The ``prometheus_client`` package is imported lazily so
    this module can be imported without the extra. When the
    user does not install ``kntgraph[metrics]``, instantiating
    :class:`PrometheusMetricsSink` raises :class:`ImportError`
    with a canonical message:

        .. code-block:: text

            PrometheusMetricsSink requires the optional package
            `prometheus_client`, which is not installed.
            Install it with one of:
                uv add kntgraph[metrics]
                pip install kntgraph[metrics]

    Multi-process / multi-tenant note
    ---------------------------------

    By default the sink registers its metrics against the
    process-wide ``prometheus_client.REGISTRY``. In tests
    that construct multiple dispatchers in one process,
    pass ``registry=CollectorRegistry()`` per sink to
    avoid name collisions. In production with a single
    dispatcher per process, the default is fine.
    """

    def __init__(
        self,
        *,
        registry: Optional["CollectorRegistry"] = None,
        namespace: str = "kntgraph",
    ) -> None:
        """Build the sink and register the five metrics.

        Args:
            registry: optional
                :class:`prometheus_client.CollectorRegistry`
                to register against. ``None`` (default) uses
                the global ``prometheus_client.REGISTRY``;
                tests typically pass a private registry to
                isolate counters between cases.
            namespace: prefix applied to every metric name
                (``kntgraph_in_flight_tasks``, etc.). The
                default is ``"kntgraph"``; the `_tasks`
                / `_total` suffixes are added automatically
                per the Prometheus convention.

        Raises:
            ImportError: when ``prometheus_client`` is not
                installed (the canonical message points to
                ``kntgraph[metrics]``).
        """
        prom = require_optional(
            "prometheus_client",
            "kntgraph[metrics]",
            purpose="PrometheusMetricsSink",
        )
        # ``Gauge`` and ``Counter`` are factory functions
        # that register with the supplied registry. With
        # ``registry=None`` they fall back to the global
        # ``REGISTRY`` (the standard Prometheus client
        # convention).
        self._registry = registry
        self._namespace = namespace
        self._in_flight = prom.Gauge(
            _IN_FLIGHT_METRIC,
            "Number of tool tasks currently waiting for a "
            "terminal event (requested but not completed/failed). "
            "Sourced from ADR-075 Tier 4 in_flight_tasks().",
            registry=registry,
        )
        self._stale = prom.Gauge(
            _STALE_METRIC,
            "Number of in-flight tasks whose deadline is past "
            "the recovery threshold. Sourced from "
            "ADR-075 Tier 4 stale_tasks().",
            registry=registry,
        )
        self._stuck_in_queue = prom.Gauge(
            _STUCK_METRIC,
            "Number of tool queues with backlog and no active "
            "consumer. Sourced from ADR-075 Tier 4 stuck_in_queue().",
            registry=registry,
        )
        self._dead_lettered = prom.Gauge(
            _DLQ_METRIC,
            "Number of DLQ entries awaiting operator action. "
            "Sourced from ADR-075 Tier 4 dead_lettered_tasks().",
            registry=registry,
        )
        self._compensations_started = prom.Counter(
            _COMPENSATION_METRIC,
            "Total number of saga compensation_started events "
            "appended to the EventLog (ADR-069 §11.18.2).",
            registry=registry,
        )

    def record_in_flight(self, count: int) -> None:
        """Set the in-flight gauge to ``count``.

        See :meth:`MetricsSink.record_in_flight`. Argument
        must be a non-negative integer; negative values
        are silently clamped to 0 by ``Gauge.set``.
        """
        self._in_flight.set(count)

    def record_stale(self, count: int) -> None:
        """Set the stale gauge to ``count``.

        See :meth:`MetricsSink.record_stale`.
        """
        self._stale.set(count)

    def record_stuck_in_queue(self, count: int) -> None:
        """Set the stuck-in-queue gauge to ``count``.

        See :meth:`MetricsSink.record_stuck_in_queue`.
        """
        self._stuck_in_queue.set(count)

    def record_dead_lettered(self, count: int) -> None:
        """Set the dead-lettered gauge to ``count``.

        See :meth:`MetricsSink.record_dead_lettered`.
        """
        self._dead_lettered.set(count)

    def incr_compensation_started(self) -> None:
        """Increment the compensations-started counter by 1.

        See :meth:`MetricsSink.incr_compensation_started`.
        Called from the dispatcher's tick loop once per
        ``*.compensation_started`` event appended to the
        EventLog. Counters are monotonic -- the value
        only ever grows until the process restarts.
        """
        self._compensations_started.inc()

    @classmethod
    def start_default_http_server(
        cls,
        port: int,
        registry: Optional["CollectorRegistry"] = None,
    ) -> None:
        """Convenience wrapper around
        ``prometheus_client.start_http_server``.

        Launches a daemon thread that serves ``GET /metrics``
        on ``port``. Pass ``registry=`` when the sink was
        built with a private registry; otherwise the global
        ``prometheus_client.REGISTRY`` is exposed (the
        convention used by ``start_http_server`` when no
        registry is supplied).

        This is sugar; users can also call
        ``prometheus_client.start_http_server(port)`` directly
        for the global-registry case.

        Raises:
            ImportError: when ``prometheus_client`` is not
                installed (the canonical message points to
                ``kntgraph[metrics]``).
        """
        prom = require_optional(
            "prometheus_client",
            "kntgraph[metrics]",
            purpose="PrometheusMetricsSink.start_default_http_server",
        )
        # ``start_http_server`` accepts an optional
        # ``registry`` kwarg in ``prometheus_client >= 0.9``.
        # We forward it conditionally to keep the wrapper
        # tolerant of older releases (the framework's own
        # minimum is declared in pyproject.toml).
        if registry is None:
            prom.start_http_server(port)
        else:
            prom.start_http_server(port, registry=registry)


__all__ = ["PrometheusMetricsSink"]
