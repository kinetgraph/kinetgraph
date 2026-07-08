# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
FMH Runner — the side-effecting orchestrator.

The runner is the only component that mutates the EventLog
(apart from external adapters that emit events). It is composed
of two cooperating coroutines:

  - Runner             : cyclic systems, run on every tick
  - ReactiveDispatcher : reactive systems, run on new events

Both rely on the EventLog for idempotency and on the World
fold for purity.
"""

from .reactive import ReactiveDispatcher
from .runner import Runner

__all__ = ["ReactiveDispatcher", "Runner"]
