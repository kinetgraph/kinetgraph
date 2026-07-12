# ADR-039: Redefining Role as a Pure ECS Component and Intent Resolution

**Status:** Accepted / Implemented

**Date:** July 11, 2026

**Version:** 1.0.0

**Authors:** Architecture Team

**Related:** ADR-006 (Superceded), ADR-017, ADR-025, ADR-034, ADR-036

---

## 1. Context

In **ADR-006**, the framework established a strict separation where `Tool` handles I/O side-effects and `Role` handles semantics. A `Role` was implemented as a Python class that wrapped an injected `Tool` to provide a domain prompt and parse the output.

While this solved I/O configuration duplication, it introduced severe architectural friction as the framework matured:

1. **Semantic Overload:** In ADR-017, we introduced `ToolACL`, which validates a principal's security role (e.g., `Role.agent`, `Role.admin`). Consequently, the word "Role" meant two completely different things: a physical RBAC security permission, and a semantic prompt wrapper.
2. 
**ECS Purity Violation:** ADR-036 mandated that no `WorldSystem` should perform blocking I/O in its `__call__` cycle. Because ADR-006 Roles were tightly coupled to Tool method invocations, they acted as impure orchestrators rather than state.


3. 
**EventLog Friction:** As noted in ADR-006, invoking a Role directly via the EventLog required adding hacky discriminators (like `"purpose": "plan"`) to the event data payload.


4. **Lack of Dynamic Orchestration:** Agents could not easily swap their cognitive personas or tool inventories at runtime because Roles were hardcoded Python instances, not data.

---

## 2. Decision

We are superceding the definition of `Role` established in ADR-006.

Moving forward, the semantic **`Role` is strictly a pure ECS Component (`dataclass`)**. It contains data about the agent's persona and permitted tool inventory, but possesses **zero behavior** and holds **no references to active network connections**.

To connect User Intents to Tool Executions without blocking I/O, we are introducing a universal, pure **`IntentResolutionSystem`**.

### 2.1 The "Role" and "Intent" Components

The semantic role and the execution intent are now defined purely as state:

```python
from dataclasses import dataclass, field
from kntgraph.core.component import Component
from kntgraph.core.event import CorrelationContext

@dataclass(frozen=True, slots=True)
class RoleComponent(Component):
    """Semantic Role defining the Agent's persona and allowed capabilities."""
    persona: str
    instructions: str
    allowed_tools: list[str] = field(default_factory=list)

@dataclass(frozen=True, slots=True)
class IntentComponent(Component):
    """Execution context Intent containing target tool, parameters, and correlation context."""
    target_tool: str
    parameters: dict
    status: str  # "pending" | "processing" | "completed" | "failed"
    correlation: CorrelationContext
```

### 2.2 The Universal `IntentResolutionSystem`

Instead of writing a custom routing system for every domain, the framework provides a data-driven resolution system. It cross-references the Agent's semantic `RoleComponent`, the physical `Principal` (L1/L2 Security), the user's `IntentComponent`, and the `ToolRegistry`.

It enforces a **Fail-Fast, Zero-Trust** policy entirely in memory, without touching the network.

```python
from kntgraph.core.world import World
from kntgraph.core.event import Event, CorrelationContext
from kntgraph.tools.registry import ToolRegistry
from kntgraph.tools.acl import default_acl
from kntgraph.security import Principal

class IntentResolutionSystem:
    """
    Pure WorldSystem: Evaluates pending intents, validates them against 
    Semantic Roles and Security ACLs, dynamically binds arguments, 
    and emits `tool.<name>.requested` events.
    """
    
    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def __call__(self, world: World) -> list[Event]:
        events = []
        
        # 1. Native ECS Lazy Query: Only process entities that possess all required components
        for agent_id, view in world.query_agents(RoleComponent, IntentComponent, Principal):
            
            intent = self._get_comp(view, IntentComponent)
            if intent.status != "pending":
                continue 
                
            role = self._get_comp(view, RoleComponent)
            principal = self._get_comp(view, Principal)
            target_tool_name = intent.target_tool

            # 2. Registry Check: Does the tool exist?
            tool = self._registry.get(target_tool_name)
            if not tool:
                events.append(self._fail_event(agent_id, view, f"Tool {target_tool_name} not found.", intent.correlation))
                continue

            # 3. Physical Security (L1/L2): Does the ToolACL allow this Principal?
            acl = self._registry.acl_for(target_tool_name) or default_acl()
            allowed, reason = acl.check(principal)
            if not allowed:
                 events.append(self._fail_event(agent_id, view, f"ACL Denied: {reason}", intent.correlation))
                 continue

            # 4. Semantic Security: Does the Agent's current Role allow this tool?
            if target_tool_name not in role.allowed_tools:
                events.append(self._fail_event(agent_id, view, "Semantic Role not authorized.", intent.correlation))
                continue
                
            # 5. Success: Emit the execution request directly (parameters validated by the Tool on execution)
            # Format MUST be tool.<name>.requested for Full Payload Fan-Out (ADR-036)
            # The "tool" key is kept in data for project_tool_calls compatibility (ADR-034)
            events.append(Event.domain_from(
                agent_id=agent_id,
                type=f"tool.{target_tool_name}.requested", 
                data={
                    "tool": target_tool_name, 
                    "params": intent.parameters
                },
                causation_id=view.last_event_id,
                correlation=intent.correlation
            ))

        return events

    def _fail_event(self, agent_id: str, view, reason: str, correlation: CorrelationContext) -> Event:
        return Event.domain_from(
            agent_id=agent_id,
            type="intent.validation_failed",
            data={"reason": reason},
            causation_id=view.last_event_id,
            correlation=correlation
        )

    def _get_comp(self, view, comp_type):
        return next(c for c in view.components.values() if isinstance(c, comp_type))

```

---

## 3. Consequences

### Pros

* **True ECS Alignment:** Agents (Entities) now "wear" Roles (Components). All orchestration logic is deferred to pure Systems. State transitions occur via archetype migrations.


* **Dynamic Hot-Swapping:** We can emit an event to swap an agent's `RoleComponent` at runtime. An agent can start as a `Researcher` and transition into a `Reviewer`, instantly altering its permitted toolset.
* **Fail-Fast Security:** Tool execution is guarded by a "Double-Lock": Semantic rules (`RoleComponent`) and Infrastructure rules (`ToolACL`). Invalid intents fail in CPU milliseconds without making costly network calls.
* 
**Flawless Routing Integration:** The emission of `tool.<name>.requested` guarantees O(1) routing by the `ToolRouter` to the `WorkerManager` queues (ADR-036) , while maintaining the ECS projection compatibility required by ADR-034.



### Cons

* **Breaking Change:** Existing Python classes under `roles/` that directly wrapped `LiteLLMTool` (from ADR-006) must be refactored into pure components.

---

## 4. Migration Strategy

1. Demote existing "Roles" from ADR-006 into `CognitiveSkills` or `PromptTemplates`.
2. Introduce `RoleComponent` to the core ECS definitions.
3. Register the `IntentResolutionSystem` in the `ReactiveDispatcher`.
4. Mark **ADR-006** as `Superceded`.