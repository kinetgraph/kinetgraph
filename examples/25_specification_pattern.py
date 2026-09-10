# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 25: Specification Pattern — Composable Business Rules (ADR-069 §2).

This example demonstrates the Specification Pattern in kinetgraph:
a pure, immutable, and composable predicate language used by Concordos
(BusinessFSM and WorkflowSaga) to express domain rules over a StepContext.

Key Concepts Demonstrated:
  1. Custom Specifications — inheriting from Specification and implementing is_satisfied_by.
  2. Built-in Specifications — DomainStateIs, ProfileTierIs, ContinuityToolUsed, StepResultEquals.
  3. All Composition Operators:
     - .and_(other) / AndSpec: Logical AND
     - .or_(other)  / OrSpec:  Logical OR
     - .not_()      / NotSpec: Logical NOT
  4. Complex Nested Rules — combining multiple specifications into domain rules.
  5. Integration with FSM Guard — using a composed specification as a transition guard.

Run with:
    PYTHONPATH=src uv run python examples/25_specification_pattern.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

from kntgraph.concordos.base import (
    AndSpec,
    NotSpec,
    OrSpec,
    Specification,
    StepContext,
)
from kntgraph.concordos.fsm import FSMConfig, FSMSystem, FSMTransition
from kntgraph.concordos.specs import (
    ContinuityToolUsed,
    ProfileTierIs,
)
from kntgraph.core.components.memory import (
    ContinuityComponent,
    ProfileComponent,
)
from kntgraph.core.world import DomainComponent
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system

FIXED_NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Custom Domain Component & Custom Specifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderDomainComponent(DomainComponent):
    """Domain component for a purchase order."""

    status: str = "draft"
    amount: float = 1000.0
    risk_score: int = 15
    credit_limit: float = 5000.0


@dataclass(frozen=True, slots=True)
class OrderAmountBelow(Specification):
    """Custom Specification: checks if order amount is below a threshold."""

    max_amount: float

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if not isinstance(ctx.domain, OrderDomainComponent):
            return False
        return ctx.domain.amount < self.max_amount


@dataclass(frozen=True, slots=True)
class RiskScoreBelow(Specification):
    """Custom Specification: checks if risk score is below a threshold."""

    max_score: int

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if not isinstance(ctx.domain, OrderDomainComponent):
            return False
        return ctx.domain.risk_score < self.max_score


@dataclass(frozen=True, slots=True)
class CustomerCreditSufficient(Specification):
    """Custom Specification: checks if customer credit limit covers the order."""

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if not isinstance(ctx.domain, OrderDomainComponent):
            return False
        return ctx.domain.credit_limit >= ctx.domain.amount


# ---------------------------------------------------------------------------
# Visual Helper
# ---------------------------------------------------------------------------


def _banner(title: str) -> None:
    print("\n" + "=" * 76)
    print(f" {title}")
    print("=" * 76)


def main() -> None:
    print("=== Specification Pattern & Composition Operators in kinetgraph (ADR-069 §2) ===")

    # Setup sample contexts
    order_low_risk = OrderDomainComponent(
        status="validating",
        amount=5000.0,
        risk_score=10,
        credit_limit=10000.0,
    )
    order_high_risk = OrderDomainComponent(
        status="validating",
        amount=60000.0,
        risk_score=85,
        credit_limit=2000.0,
    )

    profile_vip = ProfileComponent(tenant_id="t1", user_id="u1", tier="vip")
    profile_basic = ProfileComponent(tenant_id="t1", user_id="u2", tier="basic")

    continuity_fraud_alert = ContinuityComponent(
        tenant_id="t1",
        user_id="u1",
        last_tools={"fraud_alert": "2026-09-10T14:00:00Z"},
    )

    world_empty = WorldBuilder().build()

    ctx_vip_safe = StepContext(
        step_results=MappingProxyType({}),
        step_states=MappingProxyType({}),
        domain=order_low_risk,
        continuity=None,
        profile=profile_vip,
        world=world_empty,
        agent_id="agent-vip",
        now=FIXED_NOW,
    )

    ctx_basic_risky = StepContext(
        step_results=MappingProxyType({}),
        step_states=MappingProxyType({}),
        domain=order_high_risk,
        continuity=continuity_fraud_alert,
        profile=profile_basic,
        world=world_empty,
        agent_id="agent-risky",
        now=FIXED_NOW,
    )

    # -----------------------------------------------------------------------
    # 1. Basic Specifications
    # -----------------------------------------------------------------------
    _banner("1. Basic Specifications (Custom & Built-in)")

    spec_amount_10k = OrderAmountBelow(10000.0)
    spec_risk_30 = RiskScoreBelow(30)
    spec_is_vip = ProfileTierIs("vip")
    spec_fraud_alert = ContinuityToolUsed("fraud_alert")

    print(f"spec_amount_10k on vip_safe:   {spec_amount_10k.is_satisfied_by(ctx_vip_safe)}")
    print(f"spec_amount_10k on risky:      {spec_amount_10k.is_satisfied_by(ctx_basic_risky)}")
    print(f"spec_is_vip on vip_safe:       {spec_is_vip.is_satisfied_by(ctx_vip_safe)}")
    print(f"spec_fraud_alert on risky:     {spec_fraud_alert.is_satisfied_by(ctx_basic_risky)}")

    # -----------------------------------------------------------------------
    # 2. Composition via Method Chaining (.and_(), .or_(), .not_())
    # -----------------------------------------------------------------------
    _banner("2. Composition via Method Chaining (.and_(), .or_(), .not_())")

    # AND composition: Low amount AND Low Risk
    spec_safe_standard = spec_amount_10k.and_(spec_risk_30)
    print(f"AND (Low Amount & Low Risk) on vip_safe:  {spec_safe_standard.is_satisfied_by(ctx_vip_safe)}")
    print(f"AND (Low Amount & Low Risk) on risky:     {spec_safe_standard.is_satisfied_by(ctx_basic_risky)}")

    # OR composition: VIP Profile OR Sufficient Credit
    spec_vip_or_credit = spec_is_vip.or_(CustomerCreditSufficient())
    print(f"OR (VIP or Sufficient Credit) on vip_safe: {spec_vip_or_credit.is_satisfied_by(ctx_vip_safe)}")
    print(f"OR (VIP or Sufficient Credit) on risky:    {spec_vip_or_credit.is_satisfied_by(ctx_basic_risky)}")

    # NOT composition: NOT Fraud Alert Recently Triggered
    spec_no_fraud_alert = spec_fraud_alert.not_()
    print(f"NOT (Fraud Alert Used) on vip_safe:        {spec_no_fraud_alert.is_satisfied_by(ctx_vip_safe)}")
    print(f"NOT (Fraud Alert Used) on risky:           {spec_no_fraud_alert.is_satisfied_by(ctx_basic_risky)}")

    # -----------------------------------------------------------------------
    # 3. Composition via Explicit Class Constructors (AndSpec, OrSpec, NotSpec)
    # -----------------------------------------------------------------------
    _banner("3. Explicit Class Constructors (AndSpec, OrSpec, NotSpec)")

    explicit_spec = AndSpec(
        left=OrSpec(
            left=ProfileTierIs("vip"),
            right=RiskScoreBelow(20),
        ),
        right=NotSpec(
            inner=ContinuityToolUsed("fraud_alert")
        ),
    )
    print(f"Explicit ( (VIP or Low Risk) AND NOT FraudAlert ) on vip_safe: {explicit_spec.is_satisfied_by(ctx_vip_safe)}")
    print(f"Explicit ( (VIP or Low Risk) AND NOT FraudAlert ) on risky:    {explicit_spec.is_satisfied_by(ctx_basic_risky)}")

    # -----------------------------------------------------------------------
    # 4. Complex Business Validation Policy (Combined Multi-tier Rules)
    # -----------------------------------------------------------------------
    _banner("4. Complex Business Rule: Instant Approval Policy")

    # Fast-track Rule A: VIP customer buying under $50,000 with low risk
    fast_track_vip = (
        ProfileTierIs("vip")
        .and_(OrderAmountBelow(50000.0))
        .and_(RiskScoreBelow(40))
    )

    # Fast-track Rule B: Standard customer with low amount, sufficient credit, and no recent fraud alerts
    fast_track_standard = (
        OrderAmountBelow(10000.0)
        .and_(RiskScoreBelow(20))
        .and_(CustomerCreditSufficient())
        .and_(ContinuityToolUsed("fraud_alert").not_())
    )

    # Combined Auto-Approve Specification: Rule A OR Rule B
    auto_approve_policy = fast_track_vip.or_(fast_track_standard)

    print("Policy: Fast-Track VIP OR (Low Amount & Low Risk & Credit OK & NOT FraudAlert)")
    print(f"  -> Evaluation on VIP Safe Order:   {auto_approve_policy.is_satisfied_by(ctx_vip_safe)} (APPROVED)")
    print(f"  -> Evaluation on Risky Order:     {auto_approve_policy.is_satisfied_by(ctx_basic_risky)} (REJECTED)")

    # -----------------------------------------------------------------------
    # 5. Integration with BusinessFSM Transition Guard
    # -----------------------------------------------------------------------
    _banner("5. Integration as a Guard in BusinessFSM")

    order_fsm_config = FSMConfig(
        component_type=OrderDomainComponent,
        state_field="status",
        transitions={
            "validating": {
                "order.approve": FSMTransition(
                    to="approved",
                    guard=auto_approve_policy,
                ),
                "order.reject": FSMTransition(to="rejected"),
            },
        },
        terminal=frozenset({"approved", "rejected"}),
    )

    # Test FSM with VIP Safe Order
    view_vip = (
        AgentViewBuilder("agent-vip")
        .with_component(order_low_risk)
        .with_component(profile_vip)
        .with_trigger("order.approve")
        .build()
    )
    world_vip = WorldBuilder().with_agent(view_vip).build()
    events_vip = run_system(FSMSystem(order_fsm_config, now=lambda: FIXED_NOW), world_vip)

    print("Attempting 'order.approve' for VIP Safe Agent:")
    for evt in events_vip:
        print(f"  Emitted Event: {evt.event_type} (data={evt.data})")
    assert any(e.event_type == "fsm.transitioned" for e in events_vip)

    # Test FSM with Risky Order (Guard Fails -> transition_rejected)
    view_risky = (
        AgentViewBuilder("agent-risky")
        .with_component(order_high_risk)
        .with_component(profile_basic)
        .with_component(continuity_fraud_alert)
        .with_trigger("order.approve")
        .build()
    )
    world_risky = WorldBuilder().with_agent(view_risky).build()
    events_risky = run_system(FSMSystem(order_fsm_config, now=lambda: FIXED_NOW), world_risky)

    print("\nAttempting 'order.approve' for Risky Agent:")
    for evt in events_risky:
        print(f"  Emitted Event: {evt.event_type} (reason={evt.data.get('reason')})")
    assert any(e.event_type == "fsm.transition_rejected" for e in events_risky)

    print("\nAll specification pattern examples executed successfully.")


if __name__ == "__main__":
    main()
