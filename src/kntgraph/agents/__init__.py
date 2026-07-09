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
"""
