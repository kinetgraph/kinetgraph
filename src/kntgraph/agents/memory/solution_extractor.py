# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents.memory.solution_extractor -- ``SolutionExtractorSystem``.

Iter 28 FU 8 (ADR-034): a pure ``WorldSystem`` that
emits ``solution.candidate_extracted`` events for
completed tool calls.

The system is **pure**: it reads the World (which
already has ``ToolCallRequest`` and
``ToolCallCompletion`` components materialised by
``project_tool_calls``) and emits events. No I/O, no
Redis, no FalkorDB. The I/O is delegated to
``SolutionPromoterSystem`` and
``SolutionReviewPublisherSystem``.

This replaces the extract+gate portion of
``KnowledgeConsolidator`` (which has 892 LOC and
re-reads the entire EventLog every pump).
"""

from __future__ import annotations

from typing import Any

from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.event.event import Event
from kntgraph.core.world.components import (
    ToolCallCompletion,
    ToolCallRequest,
)
from kntgraph.core.world.world import World


class SolutionExtractorSystem:
    """
    Pure WorldSystem: emits solution.candidate_extracted
    events for completed tool calls.

    Reads from `world.components`:
      - "tool_completions": dict[request_id, ToolCallCompletion]
        (the source of truth for "this tool call has
        finished"; the system iterates this slot).
      - "tool_requests": dict[request_id, ToolCallRequest]
        (used to match the tool_name and params;
        may have been evicted by ADR-044 §2.3
        completion-driven eviction).

    Emits:
      - `solution.candidate_extracted` (event_class="domain")
        for each (request, completion) pair with
        completion.status == "completed".

    Cross-agent threshold: a candidate is only emitted
    when the same (tool, params_fingerprint) is observed
    in `bump_min_agents` distinct agents' completions.
    For Iter 28 FU 8 (single-tenant), the threshold is
    applied within the World; for multi-tenant, it
    could be extended to query across tenants.

    Note: ADR-034 §2.3 calls for a more sophisticated
    cross-agent signal using `params_fingerprint`. This
    initial implementation uses a simpler heuristic
    (same tool_name + same JSON-shaped params); a
    follow-up iter can swap in the fingerprint.
    """

    def __init__(
        self,
        *,
        bump_min_agents: int = 1,
        tool_allowlist: "frozenset[str] | None" = None,
    ) -> None:
        if bump_min_agents < 1:
            raise ValueError(f"bump_min_agents must be >= 1, got {bump_min_agents}")
        self._bump_min_agents = bump_min_agents
        self._tool_allowlist = tool_allowlist

    def __call__(self, world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.agents.items():
            completions: dict[str, ToolCallCompletion] = (
                view.components.get("tool_completions", {}) or {}
            )
            # Iterate over completions (the source of
            # truth for "this tool call has finished").
            # The corresponding request (with the
            # tool_name and params) is in the
            # ``tool_completion.request_event_id``
            # field, but the request itself may have
            # been evicted from the ``tool_requests``
            # slot (ADR-044 §2.3 option 1: completion-
            # driven eviction). We read the tool_name
            # and params from the request if it is
            # still there, else from the completion's
            # own ``request_event_id`` field (which is
            # the lookup key into the params of the
            # original request, when the request is
            # still in the slot).
            requests: dict[str, ToolCallRequest] = (
                view.components.get("tool_requests", {}) or {}
            )
            for req_id, comp in completions.items():
                if comp.status != "completed":
                    # Failed or unknown status: skip.
                    continue
                req = requests.get(req_id)
                if req is None:
                    # The request was evicted. We
                    # cannot match by tool_name /
                    # params. The cross-agent threshold
                    # is approximated by tool_name
                    # (best-effort); for v0.8.0 we
                    # conservatively skip these
                    # entries. A future ADR-045 (TTL
                    # eviction, ADR-044 §2.3) will
                    # change the policy.
                    continue
                if self._tool_allowlist is not None:
                    if req.tool_name not in self._tool_allowlist:
                        continue
                # Cross-agent threshold: count distinct
                # agents that have a completion with the
                # same tool_name AND same params
                # (structural equality on the JSON-shaped
                # params dict).
                cross = self._cross_agent_count(world, req, completions)
                if cross < self._bump_min_agents:
                    continue
                out.append(self._emit_candidate(agent_id, req, comp, cross))
        return out

    def _cross_agent_count(
        self,
        world: World,
        req: ToolCallRequest,
        completions_per_agent: dict[str, dict[str, ToolCallCompletion]],
    ) -> int:
        """Count distinct agents that have a completion
        matching this request's (tool_name, params).

        For Iter 28 FU 8 we count `>= 1` (the requesting
        agent itself). The full cross-agent
        params_fingerprint join is a follow-up iter.
        """
        # Single-agent signal: the requesting agent
        # has at least one match. Multi-agent: walk
        # world.agents and count distinct agents with
        # a completion that has the same tool_name
        # and the same params (shape, not just value).
        matched: set[str] = set()
        for other_agent_id, other_view in world.agents.items():
            other_requests = other_view.components.get("tool_requests", {}) or {}
            other_completions = other_view.components.get("tool_completions", {}) or {}
            for other_req_id, other_req in other_requests.items():
                if other_req.tool_name != req.tool_name:
                    continue
                if dict(other_req.params) != dict(req.params):
                    continue
                other_comp = other_completions.get(other_req_id)
                if other_comp is None or other_comp.status != "completed":
                    continue
                matched.add(other_agent_id)
        return len(matched)

    def _emit_candidate(
        self,
        agent_id: str,
        req: ToolCallRequest,
        comp: ToolCallCompletion,
        cross_count: int,
    ) -> Event:
        """Emit the ``solution.candidate_extracted``
        event. The event data is a flat dict that the
        SolutionPromoter (separate system) consumes to
        build the full ``SolutionCandidate``."""
        data: dict[str, Any] = {
            "request_event_id": req.request_event_id,
            "tool_name": req.tool_name,
            "params": dict(req.params),
            "requested_at": req.requested_at.isoformat(),
            "completion_status": comp.status,
            "latency_ms": comp.latency_ms,
            "cross_agent_count": cross_count,
        }
        if comp.result is not None:
            data["result"] = dict(comp.result)
        if comp.error is not None:
            data["error"] = comp.error
        return Event.create(
            event_type="solution.candidate_extracted",
            agent_id=agent_id,
            event_class="domain",
            data=data,
            correlation=CorrelationContext.new(),
        )


__all__ = ["SolutionExtractorSystem"]
