# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `kntgraph.agents.memory.solutions` — the Solution tier
(ADR-010 Fase 2).

The tests are pure: no FalkorDB, no Redis. They cover:

  - Value-object invariants (`Problem`, `Action`,
    `Outcome`, `ToolDescriptor`, `SolutionCandidate`).
  - Pure helpers (`fingerprint_problem`,
    `fingerprint_params`, `result_signature`,
    `params_from_requested`).
  - `SolutionExtractor.extract_from_events`:
    - happy path (request + completed).
    - failed outcome.
    - unpaired request (dropped).
    - allowlist filter.
    - tool name extraction (multi-dot names).
  - `SolutionExtractor.bump_confidence`:
    - cross-agent threshold met → +N.
    - single agent → no bump.
    - bump caps at agent count.
  - `SolutionPromotionBus` (FIFO drain).
  - `SolutionPromoter` skeleton (cumulative stats,
    `pump_once` returns a `PromoteStats`, fail-closed).
  - `extract(world)` is unsupported (logs warning,
    returns `[]`).
"""

from __future__ import annotations

import uuid

import asyncio

import pytest

from kntgraph.core.event import Event, CorrelationContext
from kntgraph.core.world import World
from kntgraph.agents.memory.solutions import (
    Action,
    Outcome,
    Problem,
    SolutionCandidate,
    SolutionExtractor,
    SolutionPromotionBus,
    SolutionPromoter,
    ToolDescriptor,
    fingerprint_params,
    fingerprint_problem,
    params_from_requested,
    result_signature,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestFingerprints:
    def test_fingerprint_problem_order_independent(self) -> None:
        a = fingerprint_problem({"x": 1, "y": 2})
        b = fingerprint_problem({"y": 2, "x": 1})
        assert a == b

    def test_fingerprint_problem_length(self) -> None:
        assert len(fingerprint_problem({"x": 1})) == 16

    def test_fingerprint_params_matches_problem(self) -> None:
        d = {"a": 1, "b": 2}
        assert fingerprint_params(d) == fingerprint_problem(d)

    def test_result_signature_dedup(self) -> None:
        assert result_signature({"status": "ok"}) == result_signature({"status": "ok"})
        # Reordered dicts have the same signature
        # (sort_keys=True under the hood).
        assert result_signature({"a": 1, "b": 2}) == result_signature({"b": 2, "a": 1})

    def test_params_from_requested_dict_passthrough(self) -> None:
        e = Event.domain_from(
            agent_id="a",
            type="tool.x.requested",
            data={"cnpj": "111", "valor": 1.0},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        params = params_from_requested(e)
        assert params == {"cnpj": "111", "valor": 1.0}

    def test_params_from_requested_empty_wraps(self) -> None:
        e = Event.domain_from(
            agent_id="a",
            type="tool.x.requested",
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        assert params_from_requested(e) == {"value": ""}


class TestEventTypeHelpers:
    def test_requested(self) -> None:
        from kntgraph.core.tool_event import (
            ToolEventKind,
            is_tool_event,
        )

        assert is_tool_event("tool.x.requested", ToolEventKind.REQUESTED)
        assert is_tool_event("tool.invoice.issue.requested", ToolEventKind.REQUESTED)
        assert not is_tool_event("tool.x.completed", ToolEventKind.REQUESTED)
        assert not is_tool_event("agent.spawned", ToolEventKind.REQUESTED)

    def test_completed(self) -> None:
        from kntgraph.core.tool_event import (
            ToolEventKind,
            is_tool_event,
        )

        assert is_tool_event("tool.x.completed", ToolEventKind.COMPLETED)
        assert is_tool_event("tool.bank.transfer.completed", ToolEventKind.COMPLETED)
        assert not is_tool_event("tool.x.requested", ToolEventKind.COMPLETED)

    def test_failed(self) -> None:
        from kntgraph.core.tool_event import (
            ToolEventKind,
            is_tool_event,
        )

        assert is_tool_event("tool.x.failed", ToolEventKind.FAILED)
        assert not is_tool_event("tool.x.completed", ToolEventKind.FAILED)

    def test_tool_name_with_dots(self) -> None:
        from kntgraph.core.tool_event import tool_name_of

        assert tool_name_of("tool.x.requested") == "x"
        assert tool_name_of("tool.invoice.issue.requested") == "invoice.issue"
        assert tool_name_of("tool.bank.transfer.completed") == "bank.transfer"

    def test_tool_name_returns_none_on_non_tool(self) -> None:
        """`tool_name_of` returns `None` for non-tool
        events (the canonical API). The legacy
        ``tool_name_from_event_type`` used to raise
        `ValueError`; the canonical API is total
        (returns `Optional[str]`)."""
        from kntgraph.core.tool_event import tool_name_of

        assert tool_name_of("agent.spawned") is None


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class TestValueObjects:
    def test_problem_requires_fingerprint(self) -> None:
        with pytest.raises(ValueError, match="fingerprint"):
            Problem(fingerprint="")

    def test_action_requires_keys(self) -> None:
        with pytest.raises(ValueError, match="request_event_id"):
            Action(
                request_event_id="",
                tool_name="t",
                params_fingerprint="p",
            )
        with pytest.raises(ValueError, match="tool_name"):
            Action(
                request_event_id="r",
                tool_name="",
                params_fingerprint="p",
            )
        with pytest.raises(ValueError, match="params_fingerprint"):
            Action(request_event_id="r", tool_name="t", params_fingerprint="")

    def test_outcome_validates_status(self) -> None:
        with pytest.raises(ValueError, match="status"):
            Outcome(status="bogus")

    def test_solution_candidate_requires_source(self) -> None:
        with pytest.raises(ValueError, match="source_agent_id"):
            SolutionCandidate(
                problem=Problem(fingerprint="x"),
                action=Action(
                    request_event_id="r",
                    tool_name="t",
                    params_fingerprint="p",
                ),
                outcome=Outcome(status="completed"),
                source_agent_id="",
            )

    def test_tool_descriptor_requires_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            ToolDescriptor(name="", description="d", input_schema_json="{}")


# ---------------------------------------------------------------------------
# SolutionExtractor
# ---------------------------------------------------------------------------


def _make_pair(agent_id: str, tool: str, data: dict) -> list[Event]:
    """Build a request + completed pair with proper causation."""
    req = Event.domain_from(
        agent_id=agent_id,
        type=f"tool.{tool}.requested",
        data=data,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    res = Event.domain_from(
        agent_id=agent_id,
        type=f"tool.{tool}.completed",
        data={
            "request_id": "r",
            "tool": tool,
            "result": {"ok": 1},
        },
        causation_id=req.event_id,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    return [req, res]


def _make_failed(agent_id: str, tool: str, data: dict) -> list[Event]:
    req = Event.domain_from(
        agent_id=agent_id,
        type=f"tool.{tool}.requested",
        data=data,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    res = Event.domain_from(
        agent_id=agent_id,
        type=f"tool.{tool}.failed",
        data={
            "request_id": "r",
            "tool": tool,
            "error": "boom",
        },
        causation_id=req.event_id,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    return [req, res]


class TestExtractorHappyPath:
    def setup_method(self) -> None:
        self.ext = SolutionExtractor(entity_extractor=None)

    def test_extracts_completed(self) -> None:
        events = _make_pair("a-1", "invoice.issue", {"cnpj": "111"})
        cands = self.ext.extract_from_events(events)
        assert len(cands) == 1
        c = cands[0]
        assert c.action.tool_name == "invoice.issue"
        assert c.outcome.status == "completed"
        assert c.confidence == 1
        assert c.source_agent_id == "a-1"

    def test_extracts_failed_with_error(self) -> None:
        events = _make_failed("a-1", "x", {"q": 1})
        cands = self.ext.extract_from_events(events)
        assert len(cands) == 1
        assert cands[0].outcome.status == "failed"
        assert cands[0].outcome.error_message == "boom"

    def test_unpaired_request_dropped(self) -> None:
        req_only = Event.domain_from(
            agent_id="a-1",
            type="tool.x.requested",
            data={"q": 1},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        assert self.ext.extract_from_events([req_only]) == []

    def test_multi_dot_tool_name(self) -> None:
        events = _make_pair("a-1", "bank.transfer", {"amount": 100})
        cands = self.ext.extract_from_events(events)
        assert len(cands) == 1
        assert cands[0].action.tool_name == "bank.transfer"

    def test_empty_input(self) -> None:
        assert self.ext.extract_from_events([]) == []

    def test_allowlist_filters(self) -> None:
        ext = SolutionExtractor(
            entity_extractor=None,
            allowlist=["bank.transfer"],
        )
        events = _make_pair("a-1", "invoice.issue", {"cnpj": "111"})
        assert ext.extract_from_events(events) == []

    def test_allowlist_includes(self) -> None:
        ext = SolutionExtractor(
            entity_extractor=None,
            allowlist=["invoice.issue"],
        )
        events = _make_pair("a-1", "invoice.issue", {"cnpj": "111"})
        assert len(ext.extract_from_events(events)) == 1

    def test_extract_world_unsupported(self) -> None:
        # The World projection doesn't carry full event
        # history; the extractor logs a warning and
        # returns `[]`. The consolidator is expected
        # to use `extract_from_events` instead.
        ext = SolutionExtractor(entity_extractor=None)
        world = World.empty()
        assert ext.extract(world) == []

    def test_default_extractor_is_heuristic(self) -> None:
        # When entity_extractor is None, the constructor
        # silently defaults to HeuristicEntityExtractor.
        ext = SolutionExtractor()
        # The default extractor is exposed via the
        # frozen-dataclass `entity_extractor` field; it
        # should be a HeuristicEntityExtractor instance.
        from kntgraph.knowledge.extraction import (
            HeuristicEntityExtractor,
        )

        assert isinstance(ext.entity_extractor, HeuristicEntityExtractor)


class TestBumpConfidence:
    def setup_method(self) -> None:
        self.ext = SolutionExtractor(entity_extractor=None)

    def test_bump_cross_agent(self) -> None:
        events: list[Event] = []
        for a in ["a-1", "a-2", "a-3"]:
            events.extend(_make_pair(a, "x", {"cnpj": "111", "val": 1}))
        cands = self.ext.extract_from_events(events)
        bumped = self.ext.bump_confidence(cands, events, min_agents=2)
        # Each candidate sees 2 other agents → +2.
        assert all(c.confidence == 3 for c in bumped)

    def test_bump_below_threshold(self) -> None:
        events: list[Event] = []
        for a in ["a-1", "a-2"]:
            events.extend(_make_pair(a, "x", {"cnpj": "111"}))
        cands = self.ext.extract_from_events(events)
        bumped = self.ext.bump_confidence(cands, events, min_agents=3)
        # 2 agents, min 3 → no bump.
        assert all(c.confidence == 1 for c in bumped)

    def test_bump_single_agent_no_bump(self) -> None:
        events = _make_pair("a-1", "x", {"cnpj": "111"})
        cands = self.ext.extract_from_events(events)
        bumped = self.ext.bump_confidence(cands, events, min_agents=2)
        # Only 1 agent → no bump.
        assert all(c.confidence == 1 for c in bumped)

    def test_bump_min_agents_validation(self) -> None:
        # The extractor itself does not validate
        # `min_agents`; the `KnowledgeConsolidator`
        # does. The extractor accepts any int >=1
        # because single-agent bumps are valid (they
        # simply produce no change). This test pins
        # the extractor's loose contract.
        events = _make_pair("a-1", "x", {"cnpj": "111"})
        cands = self.ext.extract_from_events(events)
        # `min_agents=1` is a no-op (you cannot bump
        # yourself).
        bumped = self.ext.bump_confidence(cands, events, min_agents=1)
        assert all(c.confidence == 1 for c in bumped)


# ---------------------------------------------------------------------------
# SolutionPromotionBus
# ---------------------------------------------------------------------------


class TestBus:
    def test_fifo_drain(self) -> None:
        bus = SolutionPromotionBus()
        cand = SolutionCandidate(
            problem=Problem(fingerprint="x"),
            action=Action(
                request_event_id="r",
                tool_name="t",
                params_fingerprint="p",
            ),
            outcome=Outcome(status="completed"),
            source_agent_id="a",
        )
        bus.publish(cand)
        bus.publish(cand)
        assert len(bus) == 2
        drained = bus.drain()
        assert len(drained) == 2
        assert len(bus) == 0

    def test_drain_clears(self) -> None:
        bus = SolutionPromotionBus()
        assert bus.drain() == []


# ---------------------------------------------------------------------------
# SolutionPromoter skeleton
# ---------------------------------------------------------------------------


class TestPromoterSkeleton:
    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            SolutionPromoter(tenant_id="")

    def test_upsert_increments_cumulative(self) -> None:
        promoter = SolutionPromoter(tenant_id="t-1")
        cand = SolutionCandidate(
            problem=Problem(fingerprint="x"),
            action=Action(
                request_event_id="r",
                tool_name="t",
                params_fingerprint="p",
            ),
            outcome=Outcome(status="completed"),
            source_agent_id="a",
        )
        # In Fase 3 the cumulative stats are updated
        # by `pump_once`, not by `upsert_solution`
        # directly. Drive the upsert through the bus
        # and the pump.
        bus = SolutionPromotionBus()
        bus.publish(cand)
        asyncio.run(promoter.pump_once(bus))
        stats = promoter.cumulative_stats
        assert stats.upserts == 1
        assert stats.by_tool == {"t": 1}

    def test_pump_once_drains_and_counts(self) -> None:
        bus = SolutionPromotionBus()
        promoter = SolutionPromoter(tenant_id="t-1")
        cand = SolutionCandidate(
            problem=Problem(fingerprint="x"),
            action=Action(
                request_event_id="r",
                tool_name="t",
                params_fingerprint="p",
            ),
            outcome=Outcome(status="completed"),
            source_agent_id="a",
        )
        bus.publish(cand)
        bus.publish(cand)
        stats = asyncio.run(promoter.pump_once(bus))
        assert stats.upserts == 2
        assert stats.by_tool == {"t": 2}
        assert len(bus) == 0

    def test_pump_once_empty(self) -> None:
        bus = SolutionPromotionBus()
        promoter = SolutionPromoter(tenant_id="t-1")
        stats = asyncio.run(promoter.pump_once(bus))
        assert stats.upserts == 0

    def test_fail_closed(self) -> None:
        bus = SolutionPromotionBus()
        _promoter = SolutionPromoter(tenant_id="t-1")

        class FlakyPromoter(SolutionPromoter):
            def __init__(self):
                super().__init__(tenant_id="t-1")

            async def upsert_solution(self, candidate):
                raise RuntimeError("boom")

        flaky = FlakyPromoter()
        cand = SolutionCandidate(
            problem=Problem(fingerprint="x"),
            action=Action(
                request_event_id="r",
                tool_name="t",
                params_fingerprint="p",
            ),
            outcome=Outcome(status="completed"),
            source_agent_id="a",
        )
        bus.publish(cand)
        stats = asyncio.run(flaky.pump_once(bus))
        assert stats.upserts == 0
        assert stats.failed == 1


# ---------------------------------------------------------------------------
# SolutionPromoter — Fase 3 (PII gate + projector)
# ---------------------------------------------------------------------------


class TestPromoterPiiGate:
    def test_skeleton_mode_still_works(self) -> None:
        # Without a projector, the promoter logs and
        # counts; no I/O.
        promoter = SolutionPromoter(tenant_id="t-1")
        assert promoter.has_projector is False
        cand = SolutionCandidate(
            problem=Problem(fingerprint="x", tags={}, text="NF"),
            action=Action(
                request_event_id="r",
                tool_name="t",
                params_fingerprint="p",
                params={},
            ),
            outcome=Outcome(status="completed"),
            source_agent_id="a",
        )
        bus = SolutionPromotionBus()
        bus.publish(cand)
        stats = asyncio.run(promoter.pump_once(bus))
        assert stats.upserts == 1
        assert stats.by_tool == {"t": 1}

    def test_pii_redacts_before_projector(self) -> None:
        from kntgraph.agents.tools.pii import RedactionResult

        class CapturingProjector:
            def __init__(self) -> None:
                self.captured: list[SolutionCandidate] = []

            async def upsert(self, candidate):
                self.captured.append(candidate)
                return 4

        class CapturingRedactor:
            """Stub redactor — Iter 25: a ``Callable``
            (not a ``Tool``). Invoked as
            ``await redactor(payload)`` returning a
            ``RedactionResult`` directly.
            """

            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, payload):
                self.calls += 1
                if isinstance(payload, str):
                    return RedactionResult(
                        redacted=payload + " [R]",
                        counts={"k": 1},
                        level=1,
                    )
                return RedactionResult(
                    redacted={**(payload or {}), "_r": True},
                    counts={"k": 1},
                    level=1,
                )

        proj = CapturingProjector()
        red = CapturingRedactor()
        promoter = SolutionPromoter(
            tenant_id="t-1",
            projector=proj,
            redactor=red,
        )
        cand = SolutionCandidate(
            problem=Problem(
                fingerprint="x",
                tags={},
                text="NF raw",
            ),
            action=Action(
                request_event_id="r",
                tool_name="t",
                params_fingerprint="p",
                params={"raw": "secret"},
            ),
            outcome=Outcome(status="completed"),
            source_agent_id="a",
        )
        bus = SolutionPromotionBus()
        bus.publish(cand)
        stats = asyncio.run(promoter.pump_once(bus))
        assert stats.upserts == 1
        assert red.calls == 2  # problem text + action params
        # The projector received the REDACTED candidate.
        written = proj.captured[0]
        assert written.problem.text == "NF raw [R]"
        assert written.action.params == {
            "raw": "secret",
            "_r": True,
        }

    def test_pii_failure_blocks_projector(self) -> None:
        class FailingRedactor:
            """Stub redactor — Iter 25: a ``Callable``
            that raises to exercise the fail-closed
            branch in the promoter.
            """

            async def __call__(self, payload):
                raise RuntimeError("pii tool down")

        class CapturingProjector:
            def __init__(self) -> None:
                self.calls = 0

            async def upsert(self, candidate):
                self.calls += 1
                return 4

        proj = CapturingProjector()
        promoter = SolutionPromoter(
            tenant_id="t-1",
            projector=proj,
            redactor=FailingRedactor(),
        )
        cand = SolutionCandidate(
            problem=Problem(fingerprint="x", tags={}, text="NF"),
            action=Action(
                request_event_id="r",
                tool_name="t",
                params_fingerprint="p",
                params={},
            ),
            outcome=Outcome(status="completed"),
            source_agent_id="a",
        )
        bus = SolutionPromotionBus()
        bus.publish(cand)
        stats = asyncio.run(promoter.pump_once(bus))
        # PII failed: the promoter must NOT call the
        # projector. The candidate is counted as
        # `failed` (exception path).
        assert stats.upserts == 0
        assert stats.pii_blocked == 0
        assert stats.failed == 1
        assert proj.calls == 0

    def test_projector_failure_counted(self) -> None:
        class FlakyProjector:
            async def upsert(self, candidate):
                raise RuntimeError("falkordb down")

        from kntgraph.agents.tools.pii import RedactionResult

        class OkRedactor:
            """Iter 25: a ``Callable`` redactor."""

            async def __call__(self, payload):
                if isinstance(payload, str):
                    return RedactionResult(redacted=payload, counts={}, level=1)
                return RedactionResult(redacted=payload, counts={}, level=1)

        promoter = SolutionPromoter(
            tenant_id="t-1",
            projector=FlakyProjector(),
            redactor=OkRedactor(),
        )
        cand = SolutionCandidate(
            problem=Problem(fingerprint="x", tags={}, text="NF"),
            action=Action(
                request_event_id="r",
                tool_name="t",
                params_fingerprint="p",
                params={},
            ),
            outcome=Outcome(status="completed"),
            source_agent_id="a",
        )
        bus = SolutionPromotionBus()
        bus.publish(cand)
        stats = asyncio.run(promoter.pump_once(bus))
        assert stats.upserts == 0
        assert stats.failed == 1
        assert stats.pii_blocked == 0

    def test_cumulative_stats_track_per_outcome(self) -> None:
        from kntgraph.agents.tools.pii import RedactionResult

        class OkRedactor:
            """Iter 25: a ``Callable`` redactor."""

            async def __call__(self, payload):
                if isinstance(payload, str):
                    return RedactionResult(redacted=payload, counts={}, level=1)
                return RedactionResult(redacted=payload, counts={}, level=1)

        class StubProjector:
            def __init__(self):
                self.calls = 0

            async def upsert(self, candidate):
                self.calls += 1
                return 4

        promoter = SolutionPromoter(
            tenant_id="t-1",
            projector=StubProjector(),
            redactor=OkRedactor(),
        )
        # Publish 3 candidates.
        for i in range(3):
            cand = SolutionCandidate(
                problem=Problem(fingerprint=f"x{i}", tags={}, text="NF"),
                action=Action(
                    request_event_id=f"r{i}",
                    tool_name="t",
                    params_fingerprint="p",
                    params={},
                ),
                outcome=Outcome(status="completed"),
                source_agent_id="a",
            )
            promoter._bus = SolutionPromotionBus()
            promoter._bus.publish(cand)
            asyncio.run(promoter.pump_once(promoter._bus))
        assert promoter.cumulative_stats.upserts == 3
        assert promoter.cumulative_stats.by_tool == {"t": 3}
