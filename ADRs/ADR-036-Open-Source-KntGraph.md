<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-036: Open Source Release — Rename to `kntgraph`

**Status:** Aceito
**Data:** 10 de julho de 2026
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md)

## 1. Contexto

The framework has been developed internally under
two packages:

  - `fmh_backend` (core: ECS, EventLog, Reactive
    Dispatcher, resilience, security, API gateway)
  - `fmh_agents` (vertical: LLM/cache/PII adapters,
    roles, solution extraction)

Both packages are mature: 1695+ unit tests pass,
the CI gate (`scripts/ci.py`) is green, and the
codebase is technically ready for external
contributors.

Three blockers for an open source release:

1. **Naming**: `fmh_*` is an internal codename.
   Open source needs a public name + a single
   package boundary.
2. **Refactor scope**: the two packages share
   state and have cross-imports
   (`fmh_agents.tools.protocol` re-exports
   `fmh_backend.tools.protocol`). The line
   between "framework" and "vertical adapters" is
   not always clean.
3. **Internal references**: examples, ADRs, and
   tests still carry `fmh_backend` /
   `fmh_agents` in code, env-var names, and prose.

## 2. Decisão

Rename and unify under one PyPI package: `kntgraph`.

### 2.1 New structure

```
kntgraph/                       # was: fmh_backend/ + fmh_agents/
├── src/kntgraph/
│   ├── core/, events/, infra/, knowledge/,
│   │   memory/, resilience/, runner/,
│   │   security/, stream/, testing/,
│   │   tools/, api/             # from kntgraph
│   └── agents/                  # from kntgraph.agents
│       ├── roles/, config/, knowledge/,
│       │   memory/, tools/
├── tests/                       # unit/, integration/, agents/
├── ADRs/                        # 36 ADRs (merged)
├── docs/, examples/
├── pyproject.toml               # single package, all extras merged
└── README.md
```

The `agents` sub-module keeps the vertical
adapters (LLM, cache, PII) separate from the
core framework. Users who only need the core
ECS / event-sourcing layer can install
`kntgraph` and ignore the `agents` namespace.

### 2.2 Import migration

| Old                                            | New                                          |
| ---------------------------------------------- | -------------------------------------------- |
| `from kntgraph.X import Y`                  | `from kntgraph.X import Y`                   |
| `from kntgraph.agents.X import Y`                    | `from kntgraph.agents.X import Y`            |
| `from kntgraph.tools.llm_transport import …` | `from kntgraph.tools.llm_transport import …` |
| `from kntgraph.agents.tools.protocol import Tool`    | `from kntgraph.agents.tools.protocol import Tool` |

There are **no shims** (per AGENTS.md §2 — no
compat layers). The old `fmh_backend` and
`fmh_agents` directories are removed in the
release commit; downstream consumers update
their imports in the same commit.

### 2.3 Env-var prefix

`FMH_*` is the env-var prefix (pinned in
`Settings.model_config(env_prefix="FMH_")`).
This is **unchanged** — `FMH_` is treated as a
public contract and renaming the prefix would
break every deployment.

The only rename in env vars is conceptual: the
project name changes but the prefix does not,
to preserve operational continuity.

### 2.4 License

Apache License 2.0. Chosen over MIT because:

- Explicit patent grant is valuable for an
  ECS / agent framework that touches LLM
  providers, vector stores, and graph databases
  (all of which have active patent portfolios).
- Apache 2.0 is the most common license for
  infrastructure-level Python packages
  (cf. Apache Airflow, Apache Superset, etc.).
- Compatible with the optional `fakeredis`
  (MIT) and `falkordblite` (Apache 2.0) deps
  that ship as dev extras.

## 3. Consequências

### Pro

- **Public name + single package** → easier to
  discover, install, and cite.
- **`agents` as sub-module** → users who don't
  need the vertical adapters can ignore them;
  the core framework remains installable in a
  lightweight form.
- **No shim debt** (per AGENTS.md §2): the
  release is a hard cut. The git history
  preserves the old names for archaeology.
- **ADRs merged**: 36 ADRs (originally split
  between `fmh_backend/ADRs/` and
  `fmh_agents/ADRs/`) are now under a single
  `kntgraph/ADRs/` tree.

### Con

- **Breaking import change**: every existing
  consumer (currently `fmh_app` and `fmh_office`
  in the internal monorepo) must update its
  imports. This is a one-time cost.
- **Internal monorepo coexistence**: the old
  `fmh_backend/` and `fmh_agents/` directories
  remain in the internal monorepo until
  `fmh_app` and `fmh_office` are migrated. The
  `kntgraph/` directory is a new sibling; the
  workspace's `pyproject.toml` registers it.
- **Pyright baseline regenerates** with the new
  package; the old baseline (707 errors) is
  preserved as `.pyright-baseline-fmh.json`
  for archaeology.

## 4. Migration

For downstream packages currently importing
from `fmh_backend` or `fmh_agents`:

```bash
# Find all imports to update
grep -rln "from kntgraph\|from kntgraph.agents" .

# Apply the rename in a single commit
find . -name "*.py" -exec sed -i \
  -e 's/from kntgraph\./from kntgraph./g' \
  -e 's/from kntgraph.agents\./from kntgraph.agents./g' \
  {} \;
```

The sed is mechanical (the package name is a
unique prefix). The only edge case is string
literals / docstrings that mention
`fmh_backend` or `fmh_agents` — those are
cosmetic and can be updated in follow-up
commits.

## 5. Decisões relacionadas

- [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md)
  — the architecture that this rename is built
  on top of (typed adapters, ECS purity,
  idempotency).
- [ADR-034](./ADR-034-Solution-Extraction-System.md)
  — the `SolutionExtractor` that lives under
  `kntgraph.agents.memory.solutions`.
- [ADR-035](./ADR-035-sharding-and-dispatcher-coordination-for-horizontal-scaling.md)
  — the `ReactiveDispatcher` that lives under
  `kntgraph.runner`.

## 6. Referências

- Apache License 2.0: <https://www.apache.org/licenses/LICENSE-2.0>
- AGENTS.md §2 — "Compat: zero shims".
- AGENTS.md §10 — "Documentação: docstrings
  descritivas" (prose in English).
