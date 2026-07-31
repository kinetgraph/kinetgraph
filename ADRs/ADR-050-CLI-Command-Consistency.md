<!--
SPDX-FileCopyrightText: 2026 kinetgraph
SPDX-License-Identifier: Apache-2.0
-->

# ADR-050: CLI command consistency — sub-Typer, Typer Enum, template helper

## Status

Accepted (v0.10.0 — the three decisions ship in this cycle).

## Context

The `knt` CLI lives in `src/kntgraph/cli/`. After the
v0.9.0 cleanup (DEBT §2.25 — `conftest.py` for the
optional `[cli]` extra) and the v0.10.0 ZTA work, three
inconsistencies in the CLI command surface became
visible during a code review (the user asked "o cli
possui inconsistência?" — the answer was yes, in three
places that are concrete enough to fix in a single
ADR):

1. **Mixed registration pattern.**
   `src/kntgraph/cli/main.py:22` registers `init` as a
   flat command via `app.command(name="init")(init.init)`,
   while `new` and `keys` are registered as **sub-Typers**
   (`app.add_typer(new.app, name="new")`). The help
   output reflects this asymmetry:

       $ knt --help
       Commands:
         init    Initialize a new Kinetgraph Modular Monolith repository.
         new     Generate Kinetgraph artifacts (systems, events, tools, agents).
         keys    Manage Level 1 cryptographic keys for Kinetgraph Agents.

   The author who runs `knt new system sales.X` has to
   know `new` is a *namespace*, while `init` is *flat*.
   There is no design reason for the asymmetry — `init`
   was written first, `new`/`keys` came later.

2. **Imperative validation of `--routing-mode`.**
   `init.py:39-46` lower-cases the input and checks
   membership in `valid_modes = {"external",
   "autonomous", "collaborate"}` with a hand-written
   error message. Typer supports `Enum`-typed options
   that produce the same effect declaratively AND list
   the valid choices in `--help` automatically. The
   imperative form is ~8 lines; the declarative form is
   5 lines and removes the `valid_modes` set as a
   separate source of truth.

3. **Jinja environment instantiated 7× in `new.py`.**
   The same `Environment(loader=FileSystemLoader(...))`
   is constructed once per command (`new.py:81, 128,
   174, 222, 271, 318` + `init.py:71`). Each call
   re-reads the templates directory and re-allocates
   the Jinja `Environment`. The boilerplate (`get_template`
   + `render` + `write_text`) is also duplicated
   identically. A single helper `_render_template(name,
   ctx, out_path)` collapses all 7 sites to one.

These are quality issues, not architecture issues. The
CLI is functional today; the question is whether to
fix the three and document the rationale, or wait
until a concrete adopter reports the friction.

## Decision

**Fix all three in v0.10.0, in a single commit.** Each
fix is mechanical, isolated, and covered by the
existing `tests/unit/cli/` suite plus two new tests
that document the contracts:

### 1. Sub-Typer for `init`

Move `init` from a flat command to a sub-Typer
(`init.app`) and rename the entry point to `init
project <name>`. The new surface:

```
$ knt init --help
Usage: knt init [OPTIONS] COMMAND [ARGS]...
Commands:
  project   Initialize a new Kinetgraph Modular Monolith repository.
```

Rationale: every top-level command in `knt` is a
*namespace* (`new system / new event / new tool / new
agent / new component / new context`; `keys generate`).
Making `init project` the only sub-command keeps the
mental model uniform ("`knt <verb> <noun> ...`"). The
`init` namespace also gives us room to add `init
zta-rules <path>` or `init solution-store` later
without breaking the surface (per AGENTS.md §2: new
sub-commands are not breaking changes).

Alternative considered: keep `init` flat and demote
`new` / `keys` to flat. Rejected — the `new` family
genuinely has multiple sub-commands and flattening
would force `new-system` / `new-event` etc. as
separate top-level commands (a worse surface for
discoverability and `--help`).

### 2. Typer `Enum` for `--routing-mode`

Replace the imperative `valid_modes` set with:

```python
class RoutingMode(str, Enum):
    external = "external"
    autonomous = "autonomous"
    collaborate = "collaborate"

routing_mode: RoutingMode = typer.Option(
    RoutingMode.external,
    "--routing-mode",
    case_sensitive=False,
    help="Select the intent-routing scaffold mode.",
)
```

Typer's enum handling normalises case (the
`case_sensitive=False` flag replaces the
`routing_mode.lower()` line), validates membership
(replaces the `if routing_mode not in valid_modes`
check), and prints the valid choices in `--help`
(replaces the hand-written error message). The
`RoutingMode` enum is a public type (re-exported
from `kntgraph.cli.commands.init`); the test suite
imports it to assert the canonical set.

### 3. `_render_template` helper

Extract the Jinja machinery into one helper:

```python
# kntgraph/cli/_templates.py
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ENV = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=False,  # nosec B701
)

def render_template(name: str, ctx: dict) -> str:
    """Render a Jinja template from the CLI templates dir."""
    return _ENV.get_template(name).render(ctx)
```

`init.py` and the 5 `new` commands call
`render_template(name, ctx)` and `Path.write_text`
themselves (so the helper stays I/O-free and is
trivially testable). The `_ENV` is a module-level
singleton (Jinja's `Environment` is documented as
thread-safe once `auto_reload=False`; we don't need
live-reload in a CLI).

## Consequences

### Positive

- The CLI surface is uniform: every top-level command
  is a namespace; every flag is `Enum`-typed where it
  has a closed set; every template is rendered through
  one helper.
- The `_render_template` helper removes ~30 lines of
  duplicated boilerplate from `new.py` (329 → ~300
  lines, the reduction is smaller than expected
  because the duplicated logic was tightly
  interwoven with the per-command validation, not
  the template loading).
- The `RoutingMode` enum is a single source of truth
  for the canonical set; tests assert on the enum,
  not on a string literal.
- `init project` matches the existing
  `knt <verb> <noun>` mental model, removing a
  surprising asymmetry that the next adopter would
  hit on first use.

### Negative

- `init` becomes a sub-Typer; the surface is
  `knt init project <name>` instead of `knt init <name>`.
  **Breaking change** for any operator who has
  scripted `knt init foo`. The change is small (one
  word inserted) and the CLI is pre-1.0 (v0.10.0
  ships in this cycle), so the deprecation window is
  "1 minor cycle": print a `[yellow]DeprecationWarning[/yellow]`
  when the flat form is used, remove in v0.11.0.
- The `_render_template` helper adds one
  indirection layer; readers need to follow one more
  jump. Net: a 5-line helper is cheaper to read than
  7 repetitions of a 5-line idiom.

### Neutral

- The 5 `new` commands retain their shape; the
  helper only changes the *internals* of how they
  render. The 5 separate test files
  (`test_new_system.py` etc.) are still relevant
  and pass unchanged.
- The `Enum` literal type means tests that previously
  passed `routing_mode="external"` continue to work
  (Typer coerces strings to the enum). The only test
  change is in the deprecation warning for the
  pre-ADR-050 flat form.

## Deprecation policy (one-liner)

The CLI follows a **deprecate-then-remove** lifecycle
(AGENTS.md §2). Flags and commands that survive two
minor cycles without a known adopter are eligible
for removal in the next major. This is not a new
policy — it codifies the project's existing practice
(LiteLLMTool, ToolInvoker, agents/roles were all
removed after the documented removal target) so the
"future-proof" intent is replaced by a concrete rule
the next contributor can apply.

## What this ADR does NOT cover

- Refactoring `new.py` to remove the 5× duplicated
  command body (the validation + `mkdir` + touch +
  exists-check sequence). The duplication is real
  (DEBT §2.28 candidate) but the right fix is a
  `_write_artifact` helper, not the `_render_template`
  helper, and a single helper that does both is too
  dense for one cycle.
- A `RoutingMode.from_string(...)` factory or
  alternative case-sensitivity story. The Typer
  default (`case_sensitive=False`) is sufficient
  today; if a strict-mode adopter asks, that is a
  separate decision.
- CLI version flag (`--version`). The project has
  `version = "0.10.0"` in `pyproject.toml` but no
  `--version` yet. Adding it is one line
  (`add_completion=False, no_args_is_help=True` stays
  in place); the question is whether the value comes
  from `pyproject.toml` (one read at import time) or
  from a constant. Deferred to a separate ADR if an
  adopter asks.

## Acceptance checklist

- [x] `init` registered as a sub-Typer
      (`init app`; entry point `init project <name>`).
- [x] `--routing-mode` is a `RoutingMode` enum; the
      imperative `valid_modes` set is gone.
- [x] `render_template(name, ctx)` helper exists in
      `kntgraph.cli._templates`; `init.py` and `new.py`
      call it.
- [x] `tests/unit/cli/` suite passes; the existing
      `test_init.py` tests are updated to use the
      sub-Typer surface (``init project <name>``) and
      the ``new`` bootstrap tests use the same.
- [x] CI green: 9/9 gates; pyright 0 errors.

## Deprecation note (cut from scope)

The original draft of this ADR included a deprecation
shim: ``knt init <name>`` (the pre-ADR-050 flat form)
would still work for one minor cycle and print a
DeprecationWarning, removed in v0.11.0.

The shim was cut from scope during implementation
because the Typer / sub-Typer interaction does not
allow a flat command and a sub-Typer to share the
same name without one swallowing the other (Typer
validates sub-commands before the callback fires,
so ``invoke_without_command=True`` does not catch
the flat form). A working shim would require a
custom ``sys.argv`` rewrite in the root callback,
which is fragile in test contexts (Typer's
``CliRunner`` mutates ``sys.argv`` to ``['-c']``).

The CLI follows the **breaking change in minor**
cadence instead (AGENTS.md §2: v0.10.0 IS a minor;
operators reading the CHANGELOG will see the surface
change and adjust). The deprecation policy in the
§"Deprecation policy" section above still applies
to **flags** (e.g. ``--use-intent-http`` survives
2 minor cycles) and **commands** added in a future
cycle; it just does not apply to the one-off
``init`` flat form.

If an adopter asks for the shim, a follow-up ADR
can revisit it with a different approach (e.g. a
``knt init --project-name <name>`` flag that Typer
handles natively, leaving the sub-Typer for the
canonical ``knt init project <name>`` form).

## References

  - [ADR-046: CLI Intent Routing Scaffold](./ADR-046-CLI-Intent-Routing-Scaffold.md)
    — the predecessor ADR; introduced `--routing-mode`.
  - [AGENTS.md §2](../AGENTS.md) — the deprecate-then-
    remove lifecycle this ADR codifies.
  - [DEBT.md §2.25](../DEBT.md) — the v0.9.0 CLI
    conftest fix; the precedent for surgical CLI
    cleanups.
  - [Typer Enum options](https://typer.tiangolo.com/tutorial/options/enum/)
    — the upstream pattern this ADR adopts.
