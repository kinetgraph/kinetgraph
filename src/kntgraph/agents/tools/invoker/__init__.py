# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tool invoker — bridges the pure system world and the
side-effecting tool world via the EventLog.

The flow:

  1. A SYSTEM (pure) emits `tool.{name}.requested` with the
     arguments in `data` and the original event as
     `causation_id`.

  2. An ADAPTER (a tool consumer) reads the request, calls
     the real `Tool.invoke(**kwargs)`, and emits either
     `tool.{name}.completed` (with the result in `data`) or
     `tool.{name}.failed` (with the error in `data`).

  3. Reactive systems (pure) listen to the `.completed` /
     `.failed` events and react.

The `ToolInvoker` is a small helper that automates step 2
for testing and for adapters that don't need elaborate
resilience / circuit breaker / DLQ.

Idempotency
-----------

Because `tool.{name}.requested` events are deterministic in
their id (same causation_id, same name, same args → same
event_id), the EventLog dedupes them. The adapter is invoked
AT MOST ONCE for any given (request_event_id) tuple. The
`result_event_id` (the `.completed` or `.failed` event) is
also deterministic; a re-invocation produces the same result
event, which downstream systems consume idempotently.

Package layout
--------------

* ``_invoker.py`` — the ``ToolInvoker`` class (orchestrator).
* ``_emit.py`` — module-level helpers that build and append
  ``tool.{name}.{completed,args_invalid,failed}`` events.
* ``_types.py`` — internal signal types (``ArgsInvalid``).
"""

from __future__ import annotations

from kntgraph.agents.tools.invoker._invoker import ToolInvoker
from kntgraph.agents.tools.invoker._emit import tool_name_from_request
from kntgraph.agents.tools.invoker._types import ArgsInvalid

__all__ = ["ToolInvoker", "ArgsInvalid", "tool_name_from_request"]
