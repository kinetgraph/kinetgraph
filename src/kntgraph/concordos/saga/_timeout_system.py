# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.saga._timeout_system -- saga-level timeout (ADR-069 §4.6).

``SagaTimeoutSystem`` runs on every dispatcher tick, detects
sagas that have exceeded ``saga_timeout_ms``, and emits
``saga.<name>.timed_out``. The emitted event's ``event_id``
is deterministic (via ``generate_deterministic_event_id``)
so a tick that re-derives the same timeout produces the same
event and is deduped by the EventLog's idempotency check.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from kntgraph.core.clock import injectable_clock
from kntgraph.core.event.correlation import correlation_middleware
from kntgraph.core.event.id_helpers import generate_deterministic_event_id

from ._components import SagaProgressComponent
from ._config import SagaConfig

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.clock import Clock
    from kntgraph.core.event.event import Event
    from kntgraph.core.world.world import World

__all__ = ["SagaTimeoutSystem"]


class SagaTimeoutSystem:
    """
    C-02: WorkflowSaga — WorldSystem for saga-level timeout.

    Runs on every dispatcher tick. Detects sagas that have
    exceeded ``saga_timeout_ms`` and emits
    ``saga.<name>.timed_out``.

    **Determinism.** The emitted event's ``event_id`` is
    computed by ``generate_deterministic_event_id`` from
    ``(causation_id="root", agent_id, event_type, data)``. The
    data envelope includes the saga's ``started_at`` ISO
    string — so a tick that re-derives the same timeout
    produces the same ``event_id`` and is deduped by the
    EventLog's idempotency check.

    Individual step timeouts are handled by ADR-045 (Tool Call
    TTL) and do not need to be checked here.
    """

    __slots__ = ("_configs", "_now")

    def __init__(
        self,
        configs: "Mapping[str, SagaConfig]",
        *,
        now: "Clock | None" = None,
    ) -> None:
        self._configs = configs
        self._now = injectable_clock(now)

    def __call__(self, world: "World") -> list["Event"]:
        now = self._now()
        out: list[Event] = []
        for _agent_id, view in world.query_agents(SagaProgressComponent):
            saga = view.get_component(SagaProgressComponent)
            if saga is None or saga.direction != "forward":
                continue
            config = self._configs.get(saga.saga_name)
            if config is None:
                continue
            elapsed_ms = (now - saga.started_at).total_seconds() * 1000
            if elapsed_ms <= config.saga_timeout_ms:
                continue
            data: dict[str, "JsonValue"] = {
                "saga_id": saga.saga_id,
                "elapsed_ms": elapsed_ms,
                "stuck_at_step": saga.current_step,
                "timeout_ms": config.saga_timeout_ms,
            }
            event_type = f"saga.{saga.saga_name}.timed_out"
            eid = generate_deterministic_event_id(
                causation_id="root",
                event_type=event_type,
                data=data,
                agent_id=view.agent_id,
            )
            correlation = correlation_middleware.current()
            from kntgraph.core.event.event import Event

            out.append(
                Event.create(
                    event_id=eid,
                    agent_id=view.agent_id,
                    event_type=event_type,
                    event_class="domain",
                    data=data,
                    correlation=correlation,
                )
            )
        return out
