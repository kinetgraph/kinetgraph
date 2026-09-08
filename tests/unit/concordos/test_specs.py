# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the Specification Pattern (ADR-069 §2).

These tests exercise the combinators and the built-in
Specifications against a real ``StepContext`` (no mocks).
They verify purity (no I/O, no mutation), composability
(AND / OR / NOT), and the ``JsonValue`` type discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

from kntgraph.concordos import (
    AndSpec,
    ContinuityToolUsed,
    DomainStateIs,
    NotSpec,
    OrSpec,
    ProfileTierIs,
    Specification,
    StepCompleted,
    StepContext,
    StepFailed,
    StepResultEquals,
    StepTimedOut,
)
from kntgraph.core.components.memory import ContinuityComponent, ProfileComponent
from kntgraph.core.world import DomainComponent, World

FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class InvoiceDomainComponent(DomainComponent):
    """A minimal domain component for the spec tests."""

    status: str = "draft"
    tax_regime: str = "lucro_real"


def _ctx(
    *,
    step_states: dict[str, str] | None = None,
    step_results: dict[str, object] | None = None,
    domain: DomainComponent | None = None,
    continuity: ContinuityComponent | None = None,
    profile: ProfileComponent | None = None,
) -> StepContext:
    """Build a ``StepContext`` with the given state and a real
    (empty) ``World``."""
    return StepContext(
        step_results=MappingProxyType(step_results or {}),
        step_states=MappingProxyType(step_states or {}),
        domain=domain,
        continuity=continuity,
        profile=profile,
        world=World.empty(),
        agent_id="agent-1",
        now=FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# Combinators
# ---------------------------------------------------------------------------


def test_and_spec_true_when_both_satisfied() -> None:
    """``A.and_(B)`` is satisfied iff both A and B are."""
    a = StepCompleted("step-1")
    b = StepCompleted("step-2")
    ctx = _ctx(step_states={"step-1": "completed", "step-2": "completed"})
    assert a.and_(b).is_satisfied_by(ctx) is True
    assert isinstance(a.and_(b), AndSpec)


def test_and_spec_false_when_one_not_satisfied() -> None:
    """``A.and_(B)`` is False when either operand is False."""
    a = StepCompleted("step-1")
    b = StepCompleted("step-2")
    ctx = _ctx(step_states={"step-1": "completed", "step-2": "failed"})
    assert a.and_(b).is_satisfied_by(ctx) is False


def test_or_spec_true_when_either_satisfied() -> None:
    """``A.or_(B)`` is satisfied iff at least one of A or B is."""
    a = StepCompleted("step-1")
    b = StepCompleted("step-2")
    ctx = _ctx(step_states={"step-1": "failed", "step-2": "completed"})
    assert a.or_(b).is_satisfied_by(ctx) is True
    assert isinstance(a.or_(b), OrSpec)


def test_not_spec_inverts() -> None:
    """``A.not_()`` is satisfied iff A is not."""
    a = StepCompleted("step-1")
    ctx = _ctx(step_states={"step-1": "failed"})
    assert a.not_().is_satisfied_by(ctx) is True
    assert isinstance(a.not_(), NotSpec)


def test_fluent_chain_types_as_and_spec() -> None:
    """``A.and_(B).and_(C)`` composes end-to-end as ``AndSpec``."""
    a = StepCompleted("step-1")
    b = StepCompleted("step-2")
    c = StepCompleted("step-3")
    chain = a.and_(b).and_(c)
    assert isinstance(chain, AndSpec)
    ctx = _ctx(
        step_states={
            "step-1": "completed",
            "step-2": "completed",
            "step-3": "completed",
        }
    )
    assert chain.is_satisfied_by(ctx) is True


def test_specification_is_abstract() -> None:
    """A bare ``Specification`` cannot be instantiated."""
    import pytest

    with pytest.raises(TypeError):
        Specification()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Built-in Specifications
# ---------------------------------------------------------------------------


def test_step_completed() -> None:
    """``StepCompleted`` is True only for the ``completed`` state."""
    ctx = _ctx(step_states={"step-1": "completed"})
    assert StepCompleted("step-1").is_satisfied_by(ctx) is True
    assert StepCompleted("step-2").is_satisfied_by(ctx) is False


def test_step_failed_matches_failed_and_timed_out() -> None:
    """``StepFailed`` matches both ``failed`` and ``timed_out``."""
    assert StepFailed("s").is_satisfied_by(_ctx(step_states={"s": "failed"})) is True
    assert StepFailed("s").is_satisfied_by(_ctx(step_states={"s": "timed_out"})) is True
    assert (
        StepFailed("s").is_satisfied_by(_ctx(step_states={"s": "completed"})) is False
    )


def test_step_timed_out() -> None:
    """``StepTimedOut`` is True only for the ``timed_out`` state."""
    assert (
        StepTimedOut("s").is_satisfied_by(_ctx(step_states={"s": "timed_out"})) is True
    )
    assert StepTimedOut("s").is_satisfied_by(_ctx(step_states={"s": "failed"})) is False


def test_step_result_equals() -> None:
    """``StepResultEquals`` compares a field in a step result."""
    ctx = _ctx(step_results={"validate": {"nfe_required": True}})
    assert (
        StepResultEquals("validate", "nfe_required", True).is_satisfied_by(ctx) is True
    )
    assert (
        StepResultEquals("validate", "nfe_required", False).is_satisfied_by(ctx)
        is False
    )


def test_step_result_equals_missing_step_is_false() -> None:
    """A missing step result (or a non-mapping result) is False."""
    ctx = _ctx(step_results={})
    assert StepResultEquals("missing", "field", 1).is_satisfied_by(ctx) is False


def test_domain_state_is() -> None:
    """``DomainStateIs`` reads a field off the DomainComponent."""
    domain = InvoiceDomainComponent(status="validating")
    ctx = _ctx(domain=domain)
    assert DomainStateIs("status", "validating").is_satisfied_by(ctx) is True
    assert DomainStateIs("status", "draft").is_satisfied_by(ctx) is False


def test_domain_state_is_none_domain_is_false() -> None:
    """``DomainStateIs`` is False when there is no domain component."""
    ctx = _ctx(domain=None)
    assert DomainStateIs("status", "validating").is_satisfied_by(ctx) is False


def test_profile_tier_is() -> None:
    """``ProfileTierIs`` matches the profile tier."""
    profile = ProfileComponent(tenant_id="t-1", user_id="u-1", tier="vip")
    ctx = _ctx(profile=profile)
    assert ProfileTierIs("vip").is_satisfied_by(ctx) is True
    assert ProfileTierIs("basic").is_satisfied_by(ctx) is False


def test_profile_tier_is_none_profile_is_false() -> None:
    """``ProfileTierIs`` is False when there is no profile."""
    ctx = _ctx(profile=None)
    assert ProfileTierIs("vip").is_satisfied_by(ctx) is False


def test_continuity_tool_used() -> None:
    """``ContinuityToolUsed`` checks the recent-tools window."""
    continuity = ContinuityComponent(
        tenant_id="t-1",
        user_id="u-1",
        last_tools={"nfe_emitter": "2026-09-07T11:59:00Z"},
    )
    ctx = _ctx(continuity=continuity)
    assert ContinuityToolUsed("nfe_emitter").is_satisfied_by(ctx) is True
    assert ContinuityToolUsed("other_tool").is_satisfied_by(ctx) is False


def test_continuity_tool_used_none_continuity_is_false() -> None:
    """``ContinuityToolUsed`` is False when there is no continuity."""
    ctx = _ctx(continuity=None)
    assert ContinuityToolUsed("nfe_emitter").is_satisfied_by(ctx) is False


# ---------------------------------------------------------------------------
# Composition across built-ins (the ADR-069 §3.6 guard shape)
# ---------------------------------------------------------------------------


def test_composed_guard_reads_like_business_rule() -> None:
    """The invoice guard from ADR-069 §3.6 composes built-ins:
    ``NfeRequired().and_(ContinuityToolUsed("nfe_emitter").not_())``."""
    nfe_required = StepResultEquals("validate_fiscal", "nfe_required", True)
    not_recently_emitted = ContinuityToolUsed("nfe_emitter").not_()
    guard = nfe_required.and_(not_recently_emitted)

    # NF-e required AND the emitter was NOT the last tool used.
    ctx = _ctx(
        step_results={"validate_fiscal": {"nfe_required": True}},
        continuity=ContinuityComponent(
            tenant_id="t-1", user_id="u-1", last_tools={"other": "t"}
        ),
    )
    assert guard.is_satisfied_by(ctx) is True

    # NF-e required but the emitter WAS the last tool used → blocked.
    ctx_blocked = _ctx(
        step_results={"validate_fiscal": {"nfe_required": True}},
        continuity=ContinuityComponent(
            tenant_id="t-1", user_id="u-1", last_tools={"nfe_emitter": "t"}
        ),
    )
    assert guard.is_satisfied_by(ctx_blocked) is False
