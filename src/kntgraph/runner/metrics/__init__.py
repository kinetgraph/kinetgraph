# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
runner.metrics -- concrete :class:`MetricsSink` implementations.

Each submodule corresponds to one optional backend:

  - :mod:`kntgraph.runner.metrics.prometheus` -- ``prometheus_client``
    backend, installed via ``kntgraph[metrics]``.

The framework's hot path only depends on
:class:`kntgraph.runner.MetricsSink` (the Protocol) and
:class:`kntgraph.runner.NullMetricsSink` (the no-op default).
Backend modules are imported explicitly by applications that
opt in to a particular backend.

Why a separate package
----------------------

The ``metrics`` sub-package keeps backend-specific
dependencies out of the core import graph. An application
that does not install ``kntgraph[metrics]`` still imports
the framework, instantiates :class:`ReactiveDispatcher`, and
runs the tick loop without ever loading ``prometheus_client``.

Add a new backend
-----------------

To add a new backend (e.g. StatsD, OpenTelemetry, OTLP):

1. Create ``kntgraph/runner/metrics/<backend>.py``.
2. Implement the five :class:`MetricsSink` primitives.
3. Use ``require_optional`` for any third-party import.
4. Declare the new extra in ``pyproject.toml`` and add the
   extra name to the ``all-runtime`` list.
5. Export the new backend class from this ``__init__``.

The Protocol stays the same; existing applications wire the
new backend in with ``dispatcher = ReactiveDispatcher(
..., metrics_sink=StatsdMetricsSink(host="..."))``.
"""

from __future__ import annotations

__all__: list[str] = []
