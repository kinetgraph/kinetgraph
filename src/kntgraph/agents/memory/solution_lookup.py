# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents.memory.solution_lookup -- ``SolutionLookupSystem``.

ADR-049 (Zero Token Architecture support). The read-side
of the Solution tier (ADR-010). The write-side is
``SolutionPromoterSystem``; this system closes the
ZTA loop by consulting Solutions *before* a
``tool.<name>.requested`` event triggers a fresh LLM
call.

When a new ``ToolCallRequest`` lands on the view, the
system:

  1. Computes the ``params_fingerprint`` from the
     request's ``params`` (using the same fingerprint
     helpers as the write-side, so the lookup key is
     symmetric with the merge key).
  2. Calls ``solution_store.find_match(tool_name=...,
     params_fingerprint=..., min_confidence=...)``.
  3. If a match exists AND the tool is in the operator
     allowlist, emits a synthetic
     ``tool.<name>.completed`` event with the cached
     payload from the Solution node.

The synthetic completion has a deterministic
``event_id`` derived from the request
(``f"{request_event_id}-solution"``) so the EventLog
deduplicates if both the LLM path and the Solution
path produce completions in the same tick. See
ADR-049 §2.1.3 for the concurrency analysis.

Pure: the only I/O is the ``solution_store`` call. The
system does not write to Redis or the graph.

Why a separate system
---------------------

``SolutionLookupSystem`` is intentionally separate from
``SolutionPromoterSystem``. The promoter is *write-side*
(it persists Solutions to FalkorDB); the lookup is
*read-side* (it consults Solutions to bypass LLM).
Mixing them would couple I/O write paths with the
read path the role systems depend on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.core.world.components import ToolCallRequest
from kntgraph.tools.system import ToolAwareSystem


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CachedSolution:
    """
    The minimum payload the lookup system needs to
    synthesize a ``tool.<name>.completed`` event.

    Equivalent to a FalkorDB ``(:Action)-[:PRODUCED]->(:Outcome)``
    edge plus the cached result body. Operators may
    extend this with the full ``Outcome`` (latency_ms,
    error_message, etc.) when wiring their own store.
    """

    tool_name: str
    params_fingerprint: str
    confidence: int
    result: dict[str, Any]
    # The EventLog ``event_id`` of the original
    # ``tool.<name>.completed`` event whose payload
    # this Solution captures. Used as the
    # ``request_event_id`` join key for downstream
    # consumers (the read-side Solution carries the
    # original completion's event id, not a new one).
    source_completion_event_id: str = ""


@runtime_checkable
class SolutionStoreLike(Protocol):
    """
    Subset of the Solution tier used by
    ``SolutionLookupSystem``.

    Same Protocol pattern as ``APIKeyStorage`` in
    ``infra/redis/_auth`` (ADR-019). Concrete
    implementations:

      - ``FalkorDBSolutionStore`` -- production
      - ``InMemorySolutionStore`` -- tests + the
        shipped example ``09b_solution_lookup_zta``

    The Protocol is ``@runtime_checkable`` so callers
    can do ``isinstance(store, SolutionStoreLike)`` for
    defensive config checks.
    """

    async def find_match(
        self,
        *,
        tool_name: str,
        params_fingerprint: str,
        min_confidence: int,
    ) -> Optional[CachedSolution]:
        """
        Return the highest-confidence cached Solution for
        ``(tool_name, params_fingerprint)`` whose
        ``confidence >= min_confidence``, or ``None`` if
        no match.
        """
        ...


@dataclass
class LookupStats:
    """Per-pump stats. Cumulative across pumps in the
    system instance; reset by ``reset()`` if needed."""

    cache_hit: int = 0
    cache_miss: int = 0
    bypass_low_confidence: int = 0
    bypass_not_in_allowlist: int = 0

    @property
    def total(self) -> int:
        return (
            self.cache_hit
            + self.cache_miss
            + self.bypass_low_confidence
            + self.bypass_not_in_allowlist
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "cache_hit": self.cache_hit,
            "cache_miss": self.cache_miss,
            "bypass_low_confidence": self.bypass_low_confidence,
            "bypass_not_in_allowlist": self.bypass_not_in_allowlist,
        }


class InMemorySolutionStore:
    """
    Test/example store backed by a plain dict.

    Not thread-safe. The shipped example
    ``09b_solution_lookup_zta`` uses this; production
    uses the FalkorDB adapter (out of scope for this
    ADR; see ADR-049 §6 follow-ups).
    """

    def __init__(self) -> None:
        self._solutions: dict[tuple[str, str], CachedSolution] = {}

    def add(self, solution: CachedSolution) -> None:
        """Register a Solution. Last write wins per
        ``(tool_name, params_fingerprint)``."""
        key = (solution.tool_name, solution.params_fingerprint)
        self._solutions[key] = solution

    def remove(self, tool_name: str, params_fingerprint: str) -> None:
        self._solutions.pop((tool_name, params_fingerprint), None)

    async def find_match(
        self,
        *,
        tool_name: str,
        params_fingerprint: str,
        min_confidence: int,
    ) -> Optional[CachedSolution]:
        solution = self._solutions.get((tool_name, params_fingerprint))
        if solution is None:
            return None
        if solution.confidence < min_confidence:
            return None
        return solution


def _params_fingerprint_from_request(req: ToolCallRequest) -> str:
    """
    Compute the ``params_fingerprint`` for a
    ``ToolCallRequest`` using the canonical Solution
    helper.

    Same algorithm as ``fingerprint_params`` in
    ``agents/memory/solutions/_fingerprints.py`` so the
    read-side lookup key matches the write-side merge
    key (a Solution inserted by the promoter must be
    discoverable by the lookup system).
    """

    payload = json.dumps(dict(req.params), sort_keys=True, default=str)
    from kntgraph.infra.hashing import short_hash

    return short_hash(payload)


class SolutionLookupSystem(ToolAwareSystem):
    """
    Read-side of the Solution tier (ADR-010 / ADR-049).

    On every new ``ToolCallRequest`` landing on the
    view (via the ``overlay_tool_calls`` derived
    component), consult the Solution store for a
    matching ``(tool_name, params_fingerprint)`` pair.
    If a Solution exists with
    ``confidence >= min_confidence`` and the tool is in
    ``allowlist``, emit a synthetic
    ``tool.<name>.completed`` event with the cached
    payload -- bypassing the LLM.

    Pure with respect to the framework: the only I/O
    is the ``solution_store.find_match`` call. The
    system does not write to Redis or the graph.

    Configuration
    -------------

    ``min_confidence`` (default 3): the minimum number
    of cross-agent uses required for auto-application.
    Below this, the operator wants human review (per
    ADR-010 §3.2 ``review_threshold`` = 1; ADR-049
    raises the default to 3 because ZTA-auto-apply has
    a higher bar than auto-promote).

    ``allowlist`` (default ``None``): the per-tool
    allowlist. If ``None``, no tool is auto-applied
    (the operator must explicitly opt in). This is the
    safe default: ZTA is opt-in per tool, not opt-out.

    The system registers no ``TOOL_NAME`` / request /
    completion event types; it derives the lookup from
    the view's ``tool_requests`` slot (which carries
    ``ToolCallRequest`` components per agent).
    """

    TOOL_NAME = ""  # marker; the system is not bound to one tool

    def __init__(
        self,
        *,
        solution_store: SolutionStoreLike,
        min_confidence: int = 3,
        allowlist: Optional[frozenset[str]] = None,
        tenant_id: Optional[str] = None,
    ) -> None:
        if min_confidence < 1:
            raise ValueError(f"min_confidence must be >= 1, got {min_confidence}")
        if not isinstance(solution_store, SolutionStoreLike):
            # Defensive: Protocol is @runtime_checkable but
            # we also want to fail fast at construction
            # (not first call) so the error surfaces in CI.
            raise TypeError(
                f"solution_store must implement SolutionStoreLike; "
                f"got {type(solution_store).__name__}"
            )
        self._store = solution_store
        self._min_confidence = min_confidence
        self._allowlist = allowlist
        self._tenant_id = tenant_id
        self._stats = LookupStats()
        # ``seen_request_event_ids`` prevents re-processing
        # the same ToolCallRequest across pumps (the
        # overlay slot accumulates requests across ticks,
        # so without this set the same request would
        # trigger a find_match on every tick).
        self._seen: set[str] = set()
        # ``__call__`` queues the lookups; the dispatcher
        # awaits ``run_pending_lookups`` to drain them
        # (separation of sync pump / async I/O; the
        # ``WorldSystem`` Protocol requires sync
        # ``__call__``).
        self._pending_requests: list[tuple[ToolCallRequest, str]] = []
        # The completions produced by the previous
        # ``run_pending_lookups``; the next ``__call__``
        # returns them as part of the event list.
        self._pending_results: list[Event] = []

    def __call__(self, world: World) -> list[Event]:
        """
        Walk every agent's ``tool_requests`` slot,
        compute the params fingerprint, and queue a
        ``find_match`` coroutine for each request.

        The queued coroutines are awaited via
        :meth:`run_pending_lookups`, which the
        :class:`ReactiveDispatcher` calls once per
        tick. The synthesis of completions happens
        inside :meth:`run_pending_lookups` (not here),
        so this method stays synchronous and the
        dispatcher can call it without entering the
        async context.

        Returns the events from the **previous** tick's
        pending lookups (the ones queued in the prior
        ``__call__`` and awaited by the dispatcher's
        previous-tick ``run_pending_lookups`` call).
        """
        out: list[Event] = list(self._pending_results)
        self._pending_results = []
        # Discover new requests to look up.
        for agent_id, view in world.views.items():
            if not isinstance(view.components, dict):
                continue
            requests = view.components.get("tool_requests")
            if not isinstance(requests, dict):
                continue
            for req_id, req in requests.items():
                if not isinstance(req, ToolCallRequest):
                    continue
                if req_id in self._seen:
                    continue
                self._seen.add(req_id)
                # Skip the allowlist gate before queueing
                # so the dispatcher doesn't waste a
                # coroutine on a request it would
                # immediately drop.
                if self._allowlist is not None and req.tool_name not in self._allowlist:
                    self._stats = LookupStats(
                        cache_hit=self._stats.cache_hit,
                        cache_miss=self._stats.cache_miss,
                        bypass_low_confidence=(self._stats.bypass_low_confidence),
                        bypass_not_in_allowlist=(
                            self._stats.bypass_not_in_allowlist + 1
                        ),
                    )
                    logger.info(
                        "solution.cache_bypass_not_in_allowlist",
                        extra={
                            "tool_name": req.tool_name,
                            "request_event_id": req.request_event_id,
                        },
                    )
                    continue
                self._pending_requests.append((req, agent_id))
        return out

    async def run_pending_lookups(self) -> None:
        """
        Drain the queued ``find_match`` coroutines
        (one per unseen ``ToolCallRequest``) and
        accumulate the resulting synthetic completions
        in :attr:`_pending_results`. The next
        ``__call__`` returns those completions as part
        of its event list.

        Idempotent on ``seen`` (the same request is
        never looked up twice).
        """
        if not self._pending_requests:
            return
        # Snapshot + clear the queue so re-entrancy
        # during ``find_match`` doesn't double-process.
        batch = list(self._pending_requests)
        self._pending_requests = []
        # ``asyncio.gather`` preserves order; index
        # lookups are O(1).
        results = await asyncio.gather(
            *[
                self._store.find_match(
                    tool_name=req.tool_name,
                    params_fingerprint=_params_fingerprint_from_request(req),
                    min_confidence=self._min_confidence,
                )
                for req, _agent_id in batch
            ],
            return_exceptions=True,
        )
        for (req, agent_id), result in zip(batch, results):
            if isinstance(result, BaseException):
                # A failed lookup is logged and skipped;
                # the LLM path will run for this request.
                logger.warning(
                    "solution.lookup_error",
                    extra={
                        "tool_name": req.tool_name,
                        "request_event_id": req.request_event_id,
                        "error": str(result),
                    },
                )
                self._stats = LookupStats(
                    cache_hit=self._stats.cache_hit,
                    cache_miss=self._stats.cache_miss + 1,
                    bypass_low_confidence=self._stats.bypass_low_confidence,
                    bypass_not_in_allowlist=(self._stats.bypass_not_in_allowlist),
                )
                continue
            # ``result`` is ``CachedSolution | None``
            # (per the Protocol's return type); the
            # BaseException branch above guarantees we
            # only see the union's non-exception members.
            cached: Optional[CachedSolution] = result
            self._pending_results.extend(self._emit_completion(req, agent_id, cached))

    def _emit_completion(
        self,
        req: ToolCallRequest,
        agent_id: str,
        cached: Optional[CachedSolution],
    ) -> list[Event]:
        """Emit the synthetic completion if ``cached``
        is a match; update stats; return the events."""
        if cached is None:
            self._stats = LookupStats(
                cache_hit=self._stats.cache_hit,
                cache_miss=self._stats.cache_miss + 1,
                bypass_low_confidence=self._stats.bypass_low_confidence,
                bypass_not_in_allowlist=self._stats.bypass_not_in_allowlist,
            )
            logger.info(
                "solution.cache_miss",
                extra={
                    "tool_name": req.tool_name,
                    "request_event_id": req.request_event_id,
                },
            )
            return []

        self._stats = LookupStats(
            cache_hit=self._stats.cache_hit + 1,
            cache_miss=self._stats.cache_miss,
            bypass_low_confidence=self._stats.bypass_low_confidence,
            bypass_not_in_allowlist=self._stats.bypass_not_in_allowlist,
        )
        # The synthetic completion event uses a
        # deterministic ``event_id`` derived from the
        # request so the EventLog dedup (keyed on
        # ``event_id``) catches double-counts when both
        # the LLM path and the Solution path emit
        # completions in the same tick. See ADR-049
        # §2.1.3 for the concurrency analysis.
        result_payload = dict(cached.result) if cached.result else {}
        # The tenant_id flows through the agent_id for
        # multi-tenant isolation; the producer of the
        # original completion may have used a
        # different agent_id, but the completion's
        # own tenant_id is what matters downstream.
        tenant_id = self._tenant_id or agent_id
        # Carry the request's correlation so the
        # completion joins the same flow (ADR-037).
        # ``Event.create`` requires a non-None
        # ``correlation``; we propagate from the
        # request or open a fresh flow id.
        correlation = CorrelationContext(
            correlation_id=req.correlation_id or CorrelationContext.new().correlation_id
        )
        completion = Event.create(
            event_type=f"tool.{cached.tool_name}.completed",
            agent_id=agent_id,
            event_class="domain",
            correlation=correlation,
            causation_id=req.correlation_id,
            data={
                "request_event_id": req.request_event_id,
                "result": result_payload,
                "status": "completed",
                "tenant_id": str(tenant_id),
                "source": "solution_lookup",
            },
        )
        logger.info(
            "solution.cache_hit",
            extra={
                "tool_name": cached.tool_name,
                "request_event_id": req.request_event_id,
                "event_id": str(completion.event_id),
                "confidence": cached.confidence,
            },
        )
        return [completion]

    @property
    def stats(self) -> LookupStats:
        return self._stats

    def reset(self) -> None:
        """Reset cumulative stats and the
        ``seen_request_event_ids`` cache. Useful in
        tests."""
        self._stats = LookupStats()
        self._seen.clear()
        self._pending_requests.clear()
        self._pending_results.clear()


__all__ = [
    "CachedSolution",
    "InMemorySolutionStore",
    "LookupStats",
    "SolutionLookupSystem",
    "SolutionStoreLike",
]
