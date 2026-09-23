# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
runner._metrics -- the ``MetricsSink`` Protocol + the no-op default.

The dispatcher exposes four ADR-075 Tier 4 read queries
(``in_flight_tasks`` / ``stale_tasks`` / ``stuck_in_queue`` /
``dead_lettered_tasks``) plus a saga-side signal
(``compensation_started`` events). Pushing those numbers to
a metrics backend is useful for SRE dashboards and alerts but
must NOT make ``prometheus_client`` (or any other backend)
a hard dependency of the framework.

The pattern is the same one used for the LLM adapter
(``llm`` extra), the graph client (``falkordb`` extra), and
the GLiNER entity extractor (``gliner`` extra):

  - The framework defines a small Protocol
    (:class:`MetricsSink`) with the five primitives the
    dispatcher actually emits.
  - A no-op implementation (:class:`NullMetricsSink`) is the
    default; the dispatcher works without any backend
    installed.
  - Concrete sinks (Prometheus, OpenTelemetry, statsd, ...)
    live in their own modules and are pulled in by their
    respective extras (``kntgraph[metrics]``,
    ``kntgraph[otel]``).

Operators opt in by passing the sink to ``ReactiveDispatcher``
or by calling :func:`start_default_http_server` (provided by
each concrete sink module). When no sink is passed, every
method on :class:`NullMetricsSink` returns ``None`` and the
dispatcher's hot path is unchanged.

The Protocol surface
--------------------

The five primitives mirror ADR-075's Tier 4 surface plus
the saga crash-safety marker:

  - ``record_in_flight(count)``      -- called by
    :meth:`ReactiveDispatcher.in_flight_tasks` with the
    query's result size.
  - ``record_stale(count)``          -- called by
    :meth:`ReactiveDispatcher.stale_tasks`.
  - ``record_stuck_in_queue(count)`` -- called by
    :meth:`ReactiveDispatcher.stuck_in_queue`.
  - ``record_dead_lettered(count)``  -- called by
    :meth:`ReactiveDispatcher.dead_lettered_tasks`.
  - ``incr_compensation_started()``  -- called from the
    dispatcher's tick loop once per ``*.compensation_started``
    event appended to the EventLog.

The split (query-side gauges vs. event-side counter) reflects
how the signals are produced: the four gauges are sampled by
the operator (a cron, an alert, a dashboard refresh); the
compensation counter is incremented in real time as events
flow. A single sink that exposes ``CollectorRegistry`` can
serve both at ``/metrics``; a sink that splits query and
event backends can implement each method independently.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MetricsSink(Protocol):
    """The five primitives the dispatcher emits to a metrics
    backend.

    A custom sink implements the five ``record_*`` /
    ``incr_*`` methods and may be passed as
    ``metrics_sink=`` to :class:`ReactiveDispatcher`. The
    protocol is ``runtime_checkable`` so applications can
    assert ``isinstance(obj, MetricsSink)`` defensively.

    The methods are **called from the dispatcher's query /
    event paths**, not from background threads. Sinks that
    need to push to a remote backend (StatsD, OTLP, ...)
    should buffer internally and flush on a cadence; the
    dispatcher makes no guarantees about call frequency.

    All methods are synchronous because the dispatcher's
    hot path is synchronous w.r.t. metrics. Async sinks
    should wrap their transport in ``asyncio.run_coroutine_threadsafe``
    or move the work to a background queue.
    """

    def record_in_flight(self, count: int) -> None:
        """Record the size of the ``in_flight_tasks`` query result.

        Called by :meth:`ReactiveDispatcher.in_flight_tasks`
        with the length of the returned list (the number of
        tool tasks currently waiting for a terminal event).
        """

    def record_stale(self, count: int) -> None:
        """Record the size of the ``stale_tasks`` query result.

        Subset of in-flight tasks whose ``now - expires_at``
        exceeds the recovery threshold. Spikes here are the
        canonical signal that the TTL sweeper is falling
        behind.
        """

    def record_stuck_in_queue(self, count: int) -> None:
        """Record the size of the ``stuck_in_queue`` query result.

        Number of tool queues with non-zero backlog and no
        active consumer. Sustained non-zero here means a
        worker pool is offline or undersized.
        """

    def record_dead_lettered(self, count: int) -> None:
        """Record the size of the ``dead_lettered_tasks``
        query result.

        Number of DLQ entries awaiting operator action. This
        is a leading indicator of saga compensation failures
        and tool-level unrecoverable errors.
        """

    def incr_compensation_started(self) -> None:
        """Increment the ``compensations_started`` counter.

        Called once per ``*.compensation_started`` event
        appended to the EventLog by a saga projection
        (ADR-069 §11.18.2, ADR-037). Distinct from the
        DLQ counter: a compensation can start, succeed,
        and never reach DLQ; or start and then fail and
        produce ``*.compensation_failed`` (which the DLQ
        adapter consumes).
        """


class NullMetricsSink:
    """The no-op default.

    Every method returns ``None``; the dispatcher's hot path
    is unchanged when this sink is installed. Used as the
    default value for the ``metrics_sink=`` parameter on
    :class:`ReactiveDispatcher` so applications that don't
    care about metrics do not have to import this module.
    """

    def record_in_flight(self, count: int) -> None:
        """No-op. See :meth:`MetricsSink.record_in_flight`."""
        return None

    def record_stale(self, count: int) -> None:
        """No-op. See :meth:`MetricsSink.record_stale`."""
        return None

    def record_stuck_in_queue(self, count: int) -> None:
        """No-op. See :meth:`MetricsSink.record_stuck_in_queue`."""
        return None

    def record_dead_lettered(self, count: int) -> None:
        """No-op. See :meth:`MetricsSink.record_dead_lettered`."""
        return None

    def incr_compensation_started(self) -> None:
        """No-op. See :meth:`MetricsSink.incr_compensation_started`."""
        return None


__all__ = [
    "MetricsSink",
    "NullMetricsSink",
]
