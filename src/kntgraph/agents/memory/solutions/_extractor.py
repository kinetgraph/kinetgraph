# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``SolutionExtractor`` — pure event history → list of
:class:`SolutionCandidate`.

Walks a flat event list looking for ``tool.*.requested``
events and pairs each with its ``.completed`` /
``.failed`` successor. Pure: does not touch the
EventLog, Redis, or FalkorDB.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Optional, Sequence
from uuid import UUID

from kntgraph.core.event import Event
from kntgraph.core.tool_event import (
    ToolEventKind,
    is_tool_event,
    tool_name_of,
)
from kntgraph.core.world import World
from kntgraph.knowledge.extraction.base import EntityExtractor
from kntgraph.knowledge.extraction.heuristic import HeuristicEntityExtractor
from kntgraph.agents.memory.solutions._extractor_helpers import _entities_to_tags
from kntgraph.agents.memory.solutions._fingerprints import (
    fingerprint_params,
    fingerprint_problem,
    maybe_float,
    params_from_requested,
    result_signature,
)
from kntgraph.agents.memory.solutions._values import (
    Action,
    Outcome,
    Problem,
    SolutionCandidate,
)


@dataclass(frozen=True)
class SolutionExtractor:
    """
    Pure extractor: `World → list[SolutionCandidate]`.

    The extractor walks the `World` looking for
    `tool.*.requested` events and pairs each with its
    `.completed` / `.failed` successor (matched by
    `causation_id` of the result, which points to the
    request `event_id`). When the result event is
    missing, the request is silently dropped — the
    tool call did not finish, and we don't promote
    incomplete work.

    The extractor does NOT touch the EventLog, Redis or
    FalkorDB. It is safe to call from a `CyclicSystem`
    or a post-tick orchestrator. All side effects
    (embedding, PII redaction, MERGE) belong in the
    promoter (Fase 3).

    Optional dependency: an `EntityExtractor` (heuristic
    or GLiNER2) is used to build `Problem.tags` from
    the `.requested` payload. When the extractor is
    None, `tags` is an empty dict (the heuristic is
    the default and is dependency-free).

    Frozen dataclass: the extractor is immutable. Use
    `with_allowlist(...)` to derive a new instance
    with a different tool allowlist (the typical
    operator-driven update path). The class does NOT
    read env vars; the orchestrator (KnowledgeConsolidator)
    reads them and applies the result via
    `with_allowlist`.
    """

    entity_extractor: Optional[EntityExtractor] = None
    allowlist: Optional[frozenset[str]] = None

    def __post_init__(self) -> None:
        # The `entity_extractor` field accepts `None`
        # explicitly (the caller wants to skip tag
        # extraction), a real EntityExtractor, or a
        # duck-typed object. We resolve the default
        # (None → HeuristicEntityExtractor) here and
        # validate the runtime type. The field itself
        # stays as-given so the dataclass constructor
        # remains honest about what was passed.
        if self.entity_extractor is None:
            object.__setattr__(
                self,
                "entity_extractor",
                HeuristicEntityExtractor(),
            )
        elif isinstance(self.entity_extractor, EntityExtractor):
            # Already good. Storing through
            # `object.__setattr__` would error on a
            # frozen dataclass; we only call it for
            # the None branch.
            pass
        else:
            # Surface the runtime-checkable Protocol error
            # with a useful message. The Protocol check
            # itself would also fail; this is just nicer.
            raise TypeError(
                f"entity_extractor must implement EntityExtractor, "
                f"got {type(self.entity_extractor).__name__}"
            )
        if self.allowlist is not None and not isinstance(self.allowlist, frozenset):
            # The dataclass type hint accepts
            # `Sequence[str] | None` for ergonomics, but
            # the field is normalised to frozenset on
            # construction so downstream membership
            # checks (O(1) hash) work consistently.
            object.__setattr__(
                self,
                "allowlist",
                frozenset(self.allowlist),
            )

    @classmethod
    def create(
        cls,
        entity_extractor: Optional[EntityExtractor] = None,
        *,
        allowlist: Optional[Sequence[str]] = None,
    ) -> "SolutionExtractor":
        """
        Ergonomic factory mirroring the previous
        constructor signature.

        Args:
          entity_extractor: optional `EntityExtractor`
            used to populate `Problem.tags` from the
            `.requested` payload. Defaults to
            `HeuristicEntityExtractor()` (regex, no deps).
            Pass `None` explicitly to skip tag extraction
            (faster, useful in tests where tags are not
            asserted).
          allowlist: optional CSV / list of tool names
            that are eligible for Solution promotion. When
            set, requests for tools NOT in the list are
            filtered out at extraction time. The env
            `FMH_SOLUTIONS_TOOL_ALLOWLIST` is consumed by
            `KnowledgeConsolidator`, NOT here — the
            extractor does not read env. This keeps the
            extractor pure.
        """
        return cls(
            entity_extractor=entity_extractor,
            allowlist=(frozenset(allowlist) if allowlist is not None else None),
        )

    def with_allowlist(self, allowlist: Optional[Sequence[str]]) -> "SolutionExtractor":
        """
        Return a NEW extractor with the given tool
        allowlist. Returns `self` unchanged when the
        allowlist is already identical (object equality
        on the frozenset) — a small optimisation but
        also a guard against spurious re-creation in
        the orchestrator.

        Pass `None` to clear the allowlist (every tool
        eligible). Pass a non-empty sequence to restrict
        eligibility.

        The extractor is frozen; this method is the
        only sanctioned way to change the allowlist.
        """
        new_allowlist: Optional[frozenset[str]] = (
            frozenset(allowlist) if allowlist is not None else None
        )
        if new_allowlist == self.allowlist:
            return self
        return dataclasses.replace(self, allowlist=new_allowlist)

    # ------------------------------------------------------------------ extract

    def extract(self, world: World) -> list[SolutionCandidate]:
        """
        Build candidates from a `World` snapshot.

        Algorithm:

          1. Walk `world.views` and read each agent's
             components to recover the agent's last
             domain event timestamp. The components are
             derived from the `project_default`
             projection; the agent's `domain_phase` is
             the last `event_type` it saw. The
             `components` dict contains
             `{event_type: event_data}` for the last
             domain event. (See `core/world.py:project_default`
             for the exact shape.)

          2. **Wait** — `World` only carries the
             projection, not the full event history. To
             pair requests with results, the extractor
             needs the actual events. The World fold
             returns the fold of events; we expect the
             caller (`KnowledgeConsolidator`) to pass
             the events as well.

        The pair-requiring design is delegated to
        `extract_from_events`. This method is a thin
        convenience that pulls events from the World
        when they are available; the canonical path
        is `extract_from_events(events, agents=...)`.
        """
        # The World projection doesn't carry the full
        # event history; we require the caller to pass
        # the events. The default projection stores
        # only the last domain event per agent, which
        # is not enough to pair request ↔ result.
        #
        # We log this as a misuse pattern and return
        # an empty list rather than raising — the
        # consolidator (Fase 2.5) is the only caller
        # and is expected to use the event-based API.
        import structlog

        log = structlog.get_logger()
        log.warning(
            "solution.extractor.extract_world_unsupported",
            note=(
                "World projection does not carry full event "
                "history. Use extract_from_events(events, "
                "agent_ids) instead."
            ),
        )
        return []

    def extract_from_events(
        self,
        events: Sequence[Event],
        agent_ids: Optional[Sequence[str]] = None,
    ) -> list[SolutionCandidate]:
        """
        Build candidates from a flat event list.

        `events` is the full event history (or a window
        of it) sorted by `(agent_id, timestamp)`. The
        extractor groups events by `agent_id` and then
        pairs each `tool.*.requested` with its
        matching `.completed` / `.failed` (by
        `causation_id`).

        `agent_ids` optionally restricts the extraction
        to a subset of agents. When `None`, every
        agent in `events` is processed.
        """
        if not events:
            return []

        per_agent = self._group_events_by_agent(events, agent_ids)
        out: list[SolutionCandidate] = []
        for agent_id, agent_events in per_agent.items():
            result_by_request, requests = self._index_results_and_requests(agent_events)
            for req in requests:
                result = result_by_request.get(req.event_id)
                if result is None:
                    # No result yet — the tool call didn't
                    # finish. Don't promote incomplete work.
                    continue
                cand = self._build_candidate(agent_id, req, result)
                if cand is not None:
                    out.append(cand)
        return out

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _group_events_by_agent(
        events: Sequence[Event],
        agent_ids: Optional[Sequence[str]],
    ) -> dict[str, list[Event]]:
        """
        Group events by `agent_id`, optionally filtering
        to a subset of agents. When `agent_ids` is None,
        every event is kept.
        """
        agents_filter: Optional[set[str]] = (
            set(agent_ids) if agent_ids is not None else None
        )
        per_agent: dict[str, list[Event]] = {}
        for e in events:
            if agents_filter is not None and e.agent_id not in agents_filter:
                continue
            per_agent.setdefault(e.agent_id, []).append(e)
        return per_agent

    @staticmethod
    def _index_results_and_requests(
        agent_events: Sequence[Event],
    ) -> "tuple[dict[UUID, Event], list[Event]]":
        """
        Walk the per-agent event list and produce two
        collections:

          - `result_by_request`: maps each
            `.completed`/`.failed` event's
            `causation_id` (the request's `event_id`)
            to the result event.
          - `requests`: the `.requested` events in
            order.

        Separated so `extract_from_events` stays
        flat (CC ≤ 6) and the indexer is testable in
        isolation.
        """
        result_by_request: dict[UUID, Event] = {}
        requests: list[Event] = []
        for e in agent_events:
            if is_tool_event(
                e.event_type,
                ToolEventKind.COMPLETED,
                ToolEventKind.FAILED,
            ):
                if e.causation_id is not None:
                    result_by_request[e.causation_id] = e
            elif is_tool_event(e.event_type, ToolEventKind.REQUESTED):
                requests.append(e)
        return result_by_request, requests

    # ------------------------------------------------------------------ bump

    def bump_confidence(
        self,
        candidates: Sequence[SolutionCandidate],
        events: Sequence[Event],
        *,
        min_agents: int = 2,
    ) -> list[SolutionCandidate]:
        """
        Bump `confidence` for candidates whose
        `(problem_fingerprint, params_fingerprint)`
        pair appears in `min_agents` distinct agents
        in the event history.

        Cross-agent (ADR-010): the same Problem
        fingerprint seen across agents is a stronger
        signal of "this is a robust solution". A
        cross-agent match raises `confidence` by 1 per
        matching extra agent (capped at the total
        count). Single-agent matches are not bumped
        (an agent repeating its own pattern is not the
        same signal).

        Pure: reads the events, returns new
        `SolutionCandidate` objects (the originals are
        not mutated — the dataclass is frozen).

        Args:
          candidates: the candidates returned by
            `extract_from_events`.
          events: the same event history used for
            extraction (or a larger window).
          min_agents: bump step. Default `2` — seen in
            2+ distinct agents → +1 confidence. With
            `min_agents=3`, the bump only fires for 3+
            distinct agents.
        """
        if not candidates or min_agents < 2:
            return list(candidates)

        # Build the cross-agent index: for each
        # (problem_fingerprint, params_fingerprint) pair,
        # collect the set of agent_ids that have it.
        pair_agents: dict[tuple[str, str], set[str]] = {}
        for e in events:
            if not is_tool_event(e.event_type, ToolEventKind.REQUESTED):
                continue
            params = params_from_requested(e)
            pf = fingerprint_problem(params)
            apf = fingerprint_params(params)
            pair_agents.setdefault((pf, apf), set()).add(e.agent_id)

        out: list[SolutionCandidate] = []
        for c in candidates:
            key = (c.problem.fingerprint, c.action.params_fingerprint)
            agents = pair_agents.get(key, set())
            # The candidate's own agent is in `agents`;
            # the bump counts the OTHER agents.
            other_agents = len(agents - {c.source_agent_id})
            if other_agents + 1 >= min_agents:
                bump = other_agents + 1 - 1  # the candidate's own agent
                if bump > 0:
                    out.append(
                        SolutionCandidate(
                            problem=c.problem,
                            action=c.action,
                            outcome=c.outcome,
                            source_agent_id=c.source_agent_id,
                            confidence=c.confidence + bump,
                            source_request_event_id=c.source_request_event_id,
                            source_result_event_id=c.source_result_event_id,
                        )
                    )
                    continue
            out.append(c)
        return out

    # ------------------------------------------------------------------ helpers

    def _build_candidate(
        self,
        agent_id: str,
        request: Event,
        result: Event,
    ) -> Optional[SolutionCandidate]:
        """
        Build a single `SolutionCandidate` from a
        request/result pair. Returns `None` when the
        request is filtered out (e.g. tool not in the
        allowlist).
        """
        if not is_tool_event(request.event_type, ToolEventKind.REQUESTED):
            return None
        if not is_tool_event(
            result.event_type,
            ToolEventKind.COMPLETED,
            ToolEventKind.FAILED,
        ):
            return None

        tool_name = tool_name_of(request.event_type)
        if self.allowlist is not None and tool_name not in self.allowlist:
            return None

        # Problem: fingerprint + tags from the request data.
        params = params_from_requested(request)
        problem_fp = fingerprint_problem(params)
        problem_text = json.dumps(dict(params), sort_keys=True, default=str)
        tags = self._extract_tags(problem_text)
        problem = Problem(
            fingerprint=problem_fp,
            tags=tags,
            text=problem_text,
        )

        # Action: request_event_id + params_fingerprint.
        action = Action(
            request_event_id=str(request.event_id),
            tool_name=tool_name,
            params_fingerprint=fingerprint_params(params),
            params=dict(params),
        )

        # Outcome: status, latency, result_signature, error.
        status = (
            "completed"
            if is_tool_event(result.event_type, ToolEventKind.COMPLETED)
            else "failed"
        )
        latency = maybe_float(result.data.get("latency_ms"))
        if status == "completed":
            sig = result_signature(result.data.get("result"))
            err: Optional[str] = None
        else:
            sig = ""
            err_raw = result.data.get("error")
            err = str(err_raw) if err_raw is not None else None
        outcome = Outcome(
            status=status,
            latency_ms=latency,
            result_signature=sig,
            error_message=err,
        )

        return SolutionCandidate(
            problem=problem,
            action=action,
            outcome=outcome,
            source_agent_id=agent_id,
            confidence=1,
            source_request_event_id=str(request.event_id),
            source_result_event_id=str(result.event_id),
        )

    async def _extract_tags_async(self, text: str) -> dict[str, str]:
        return self._extract_tags(text)

    def _extract_tags(self, text: str) -> dict[str, str]:
        """
        Run the entity extractor over the problem
        text and convert the result to a flat
        `{key: value}` dict.

        The extractor (`EntityExtractor.extract`) is
        async; this method is called from the
        synchronous `_build_candidate`. We bridge
        the two with `asyncio.run` when no loop is
        running. In production the consolidator is
        the only caller and uses an async API
        (`extract_from_events` from a coroutine) — the
        sync path here is for tests and one-shot
        scripts.

        Schema:
          - `cnpj` (first id whose name is a CNPJ-shaped
            string) → `cnpj`.
          - `supplier` (first org) → `supplier`.
          - `uf` (location 2 chars) → `uf`.
          - `date` (date type) → `date`.
          - `money` (money type) → `amount`.
        """
        if self.entity_extractor is None:
            return {}
        import asyncio

        try:
            asyncio.get_running_loop()
            # A loop is already running (we are inside
            # a coroutine). The caller is expected to
            # use the async path; this sync helper
            # cannot run a coroutine safely. Return
            # empty tags rather than blocking the
            # event loop.
            return {}
        except RuntimeError:
            # No running loop — safe to `asyncio.run`.
            pass
        entities = asyncio.run(self.entity_extractor.extract(text))
        return _entities_to_tags(entities)
