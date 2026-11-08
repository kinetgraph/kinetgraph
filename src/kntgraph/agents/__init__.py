# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents — vertical agents sobre o framework FMH.

Esta package provê:

  - tools/    : adapters concretos (LiteLLM, ...)
  - roles/    : especializações semânticas que usam tools
                (Planner, Summarizer, Classifier, ...)
  - config/   : configuração carregada de env (modelos, budgets, ...)
  - examples/ : scripts demonstrativos (estudo de APIs)

Convenções
----------

- **Tool** = 1 capability de I/O. Vive em `tools/`. Registrável
  no `ToolRegistry`. Implementa o Protocol de
  `kntgraph.agents.tools.protocol.Tool`.
- **Role** = especialização semântica. Vive em `roles/`. Não é
  Tool — usa uma Tool por injeção. Conhece prompt do domínio e
  schema de saída.

- **agent_id**: a aplicação define. Para NF, o número da NF. Para
  sessão, "session:<id>". Para Empresa, CNPJ. O framework é
  agnóstico.

- **idempotency_key**: toda Tool recebe
  `idempotency_key=str(request.event_id)`. Roles devem
  repassar essa chave (ou construir uma estável) ao chamar
  a Tool.

Veja:
  - ADR-006: separação Tool × Role
  - ADR-007: LLM via LiteLLM

Declarative agent model (ADR-088)
---------------------------------

Above the tools and the roles, this package provides the
**declarative model of an agent**: the developer fills one frozen
dataclass and the framework derives the installable runtime from it.

  - ``RoleSpec``      -- persona and capability policy.
  - ``AgentDefinition`` -- the agent: persona, role event type, FSM,
    sagas and the domain types its rules read.
  - ``AgentBundle``   -- the derived runtime (systems + projections).
  - ``RoleProjection`` -- materialises ``RoleComponent`` from the
    role event stream.

    definition = AgentDefinition(
        name="checkout",
        persona=RoleSpec(
            persona="checkout",
            allowed_tools=("reserve_inventory", "charge_payment"),
        ),
        role_event_type="role.assigned",
        fsm=BusinessFSMConcordo(config=fsm_config),
        sagas=(WorkflowSagaConcordo(config=saga_config),),
    )

    definition.install(dispatcher, principal=principal)

See ADR-088 and ``docs/declarative_agent_modelling.md``.
"""

from kntgraph.agents._bundle import AgentBundle
from kntgraph.agents._definition import AgentDefinition, RoleSpec
from kntgraph.agents._projection import RoleProjection

__all__ = [
    "AgentBundle",
    "AgentDefinition",
    "RoleProjection",
    "RoleSpec",
]
