# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 21: Domain Memory (ECS Components)
------------------------------------------

This example demonstrates how to implement the Domain Memory tier
using Kinetgraph's pure ECS architecture (ADR-059).

Domain Memory is used for durable, structural business facts
that should not expire. Kinetgraph provides an auto-registry 
feature via the `DomainComponent` base class. Simply declaring 
the class and passing `event_type` automatically teaches the 
framework how to hydrate it.
"""

from dataclasses import dataclass
from typing import Optional

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import AgentView, DomainComponent, World, domain_component


# =====================================================================
# 1. Define the Domain Component (ECS Auto-Registered)
# =====================================================================

@domain_component("onboarding.company_size.loaded")
@dataclass(frozen=True, slots=True)
class CompanySizeProjection(DomainComponent):
    """
    A pure ECS component representing a business fact.
    Because it inherits from DomainComponent and defines `event_type`,
    the framework automatically hydrates it during the default fold!
    """
    company_size: str  # e.g., 'MEI', 'ME', 'EPP'


# =====================================================================
# 2. Example Execution
# =====================================================================

def main() -> None:
    agent_id = "onboarding-123"
    correlation = CorrelationContext.new(correlation_id="corr-1")

    # Simulate the EventLog history
    history = [
        Event.create(
            event_type="agent.spawned",
            event_class="lifecycle",
            agent_id=agent_id,
            data={},
            correlation=correlation,
        ),
        # This event matches the `event_type` declared in our DomainComponent
        Event.create(
            event_type="onboarding.company_size.loaded",
            event_class="domain",
            agent_id=agent_id,
            data={"company_size": "MEI"},
            correlation=correlation,
        ),
        Event.create(
            event_type="onboarding.operational_data.submitted",
            event_class="domain",
            agent_id=agent_id,
            data={"billing": 5000},
            correlation=correlation,
        ),
    ]

    print("Rebuilding the World from the EventLog...")
    
    # We just run the standard fold. The framework reads the registry
    # and automatically extracts `CompanySizeProjection`!
    world = World.fold(history)
    
    view = world.get_agent(agent_id)
    if not view:
        return

    # Extract the component with perfect type-safety
    comp: Optional[CompanySizeProjection] = view.get_component(CompanySizeProjection)
    
    if comp:
        print(f"Success! The agent's company size is permanently: {comp.company_size}")
        if comp.company_size == "MEI":
            print("Applying MEI business rules...")
    else:
        print("Component not found!")


if __name__ == "__main__":
    main()
