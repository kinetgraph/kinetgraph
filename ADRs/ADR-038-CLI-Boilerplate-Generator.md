<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR 038: CLI Boilerplate Generator

## Status
Accepted

## Context
Developing applications with Kinetgraph requires adhering to strict architectural patterns. These include pure `WorldSystem` functions (ADR-018), immutable `@dataclass` ECS components (ADR-034), required correlation contexts (ADR-037), and zero-trust security key management (ADR-016). 

Currently, developers manually copy-paste existing code or write from scratch when creating new agents, events, systems, or tools. This repetitive manual work slows down productivity, increases cognitive load, and introduces the risk of human error (e.g., forgetting a frozen dataclass, missing correlation propagation, or implementing a stateful system).

To improve Developer Experience (DX) and enforce architectural consistency automatically, Kinetgraph needs a standard Command Line Interface (CLI) tool for scaffolding.

## Decision
We will build a first-party CLI tool (invoked via `knt`) to generate boilerplate code for Kinetgraph primitives. 

### Technology Stack
- **CLI Framework**: We will use **Typer** (a modern, fast, and type-safe CLI library for Python built on Click).
- **Terminal UI**: **Rich** for colorized outputs and tables.
- **Templating**: Minimal `Jinja2` or standard Python template strings to ensure generated code is compliant with the repository's `ruff` formatting rules.

### Supported Commands

The CLI will expose an `init` command for bootstrapping repositories, a `new` command group for code generation, and a `keys` group for security utilities:

1. **Project Initialization (`knt init <project-name>`)**
   - Bootstraps a complete Kinetgraph project repository from scratch.
   - Generates the standard directory structure (e.g., `src/`, `tests/`) along with a minimal runnable application skeleton (`main.py`).
   - Creates a valid `pyproject.toml` with the necessary Kinetgraph dependencies.
   - Sets up initial logging, configuration, and a basic testing skeleton.

#### Proposed Directory Structure
When running `knt init`, the CLI will scaffold a Domain-Driven structure tailored for Kinetgraph's ECS and Event Sourcing primitives:

```text
my_project/
├── pyproject.toml
├── .env.example
├── src/
│   └── my_project/
│       ├── __init__.py
│       ├── main.py              # Entrypoint (EventLog config & Runner loop)
│       ├── components/          # knt new component -> adds here
│       ├── events/              # knt new event     -> adds here
│       ├── systems/             # knt new system    -> adds here
│       ├── tools/               # knt new tool      -> adds here
│       └── agents/              # Agent orchestration and policy configs
└── tests/
    ├── conftest.py
    ├── unit/
    └── integration/
```

**Architecture Explanation**:
- **`core/`**: The Shared Kernel. Contains cross-cutting concerns that apply globally across the application, such as framework bootstrapping (EventLog initialization), logging, base configuration (Pydantic models), and shared authentication utilities.
- **`contexts/`**: The Modular Monolith boundaries. Each folder under `contexts/` represents a distinct Bounded Context (e.g., `sales`, `logistics`). 
- **Context Isolation**: A context encapsulates its own events, components, systems, tools, and agents. Agents are context-specific orchestrators that map pure `WorldSystem`s to impure `Tool`s and enforce L2 capability policies for that specific domain.
- **`main.py`**: The central entry point. It wires up the `EventLog`, initializes the `ReactiveDispatcher`, and registers the agents exported by each bounded context.

2. **Systems (`knt new system <Name>System`)**
   - Generates a valid pure-function `WorldSystem`.
   - Scaffolds the standard type hints and `yield` logic for appending components to the `World`.
   
2. **Components (`knt new component <Name>`)**
   - Generates an immutable ECS component (`@dataclass(frozen=True, slots=True)`).

3. **Events (`knt new event <namespace>.<name>`)**
   - Scaffolds event factory functions ensuring compliance with `ADR-037` (mandatory correlation propagation via `correlation_middleware`).

4. **Tools (`knt new tool <Name>`)**
   - Scaffolds a new tool conforming to the Kinetgraph standard tool protocol.

5. **Agents (`knt new agent <Name> --context <context>`)**
   - Scaffolds the orchestration file for an autonomous agent.
   - Generates the L2 `CapabilityPolicy` configuration.
   - Wires the selected `WorldSystem`s and `Tool`s to the `ReactiveDispatcher` setup.

6. **Security Keys (`knt keys generate --agent-id <id>`)**
   - Wraps `generate_keypair()` from `kntgraph.security.keys`.
   - Automatically outputs PEM files or prints them securely to stdout, allowing developers to bootstrap Level 1 (L1) zero-trust environments without writing custom Python key-generation scripts.

### Installation & Packaging
The CLI will be packaged with the framework and exposed as an optional extra. In `pyproject.toml`, it will be registered as a console script:
```toml
[project.scripts]
knt = "kntgraph.cli.main:app"

[project.optional-dependencies]
cli = ["typer", "rich"]
```

## Consequences

### Positive
- **Higher Productivity**: Drastically reduces the "time-to-first-event" for developers starting a new project or adding features.
- **Enforced Consistency**: Guarantees that newly generated code is compliant with the latest ADRs (e.g., pure systems, correlation contexts) by default.
- **Interactive Documentation**: The `knt --help` commands will guide developers, acting as discoverable documentation for the framework's primitives.

### Negative
- **Maintenance Burden**: Code generation templates must be actively maintained and updated whenever the underlying framework APIs evolve.
- **Dependency Surface**: Introduces new dependencies (`typer`, `rich`), though mitigated by placing them behind an optional `[cli]` install flag.
