---
name: kntgraph-style
description: Use when choosing identifiers, writing docstrings, or commenting kntgraph code. Covers the English-prose rule, the domain-driven identifier convention (English for natural concepts, PT-BR-derived names where the domain uses them: ContinuityManager, CNPJ, PIX), and the SQLite-style documentation discipline (every function and every variable documented, concisely, for people not yet born). Trigger keywords: docstring, comment, identifier, naming convention, prose, English, Portuguese, document variables, SQLite-style documentation.
---

<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->


# Style

## 4.6 Prose in English; identifiers follow the domain

Docstrings, comments, and module-level docs are in **English**. Identifiers follow the domain:

- **English where natural**: `ReactiveDispatcher`, `SolutionExtractor`.
- **PT-BR-derived names where the domain uses them**: `ContinuityManager`, `CNPJ` in the schema, `PIX` in the example tools.

## Every function and every variable is documented

The default on this project is **to document**, not to omit. Borrowing the SQLite house style:

1. **Every function**: a docstring that describes the function's **purpose**, not its mechanics. Read the docstring and you should know what the function is for, what it takes, and what it produces, without reading the body.
2. **Every variable**: a comment that describes **what the variable represents**, not what it is. Module-level constants, dataclass fields, class attributes, and function-local variables are all in scope. Trivial loop counters (`i`, `_`) and obvious inits are exempt; anything whose name is not already a sentence is documented.
3. **Concise**: no boilerplate. Do not restate the signature, do not paste the type, do not write "this function does X" and then "this function does X" again inside the body. One narrative paragraph per function; one short clause per variable.
4. **For people not yet born**: the bar is that a reader with no project context — fresh hire, future maintainer, future you — can pick up the file cold and understand every name. If a name alone is not enough, the comment is mandatory.
5. **Why this is non-negotiable**: natural language and formal language activate different brain pathways. The code is the formal artifact; the prose is the only thing that gives a reader the **intent** the formal artifact cannot convey. SQLite treats this as a quality lever, not a nicety. The discipline is the same here.

The bar is **prose that teaches**, not prose that decorates. If a docstring only restates the signature, drop it; if a comment only names the obvious, drop it. The rule is "document until the reader can explain the file to someone else without reading the code".

## Clear, objective, comprehensive — for readers of different generations

The prose must work for **three audiences at once**: a new hire who has never
seen the codebase, a maintainer who knows half of it, and the author in
two years who has forgotten the reasoning. That constrains how it is
written.

### Clear — the reader does not have to reconstruct

1. **Lead with the answer.** The first sentence says what the
   function is *for*, not how it works. The "how" belongs to
   the body, where the code shows it.
2. **One idea per sentence.** If a sentence carries two
   unrelated claims ("X does A, and also B"), split it. A
   reader who disagrees with one claim can still use the other.
3. **Concrete over abstract.** "The reaper reclaims a request
   whose tool is still running" beats "the reaper handles
   contention" — the first can be checked against the code, the
   second cannot.
4. **No cross-references the reader must chase.** A bare
   ``see §3.2`` moves the question rather than answering it.
   State the fact, then cite the section as support.
5. **No undefined jargon.** If a term is not in the standard
   vocabulary of the reader's generation, define it on first
   use. An ADR reference is *not* a definition — the reader may
   not have it, and the next generation never will.

### Objective — the prose says what is, not what should feel

6. **State facts, not judgements.** "Raises on a malformed
   payload" is objective. "Correctly handles malformed
   payloads" smuggles a verdict; the reader cannot verify it
   and has to trust you.
7. **No "simply", "just", "obviously", "of course".** These
   words mark a gap in the explanation as if closing it. If it
   is simple, the explanation shows it; if it is not, the
   adjective is a lie the reader will discover.
8. **Quantify what can be quantified.** "Five minutes",
   "one round-trip", "at most ten messages" — a number is
   checkable, "short" and "rarely" are not.
9. **Say what the code does, not what it was meant to do.**
   Intent that did not survive the implementation is a bug
   report, not documentation.

### Comprehensive — nothing that matters is left implicit

10. **Every contract the caller depends on.** The return
    value's meaning, the exceptions raised, whether the input is
    mutated, whether the call is idempotent, whether the
    function is safe to call concurrently.
11. **Every non-obvious decision.** A magic number, an
    ordering constraint, a deliberate deviation from the
    obvious implementation — say why. The *why* is the part a
    future reader cannot reconstruct from the code.
12. **Every surprising interaction.** What this does to the
    rest of the system, what it assumes about other modules,
    what breaks if the assumption stops holding.
13. **The edge cases the code actually handles** — the
    empty input, the already-elapsed deadline, the value that
    could be `None`. If the code branches on it, the prose
    says so.

### The tension between clear and comprehensive, and how to resolve it

Clear and comprehensive pull against each other: a long
docstring is harder to read. The resolution is **layering**,
not compromise.

- **The docstring carries the contract** — what it takes,
  what it returns, what it raises, what the caller must know.
  This is what a reader needs *before* reading the body.
- **The body carries the mechanism** — how it is done, with
  inline comments only where the code would otherwise be
  puzzling.
- **The comment carries the non-derivable** — the reason,
  the constraint, the history. Never a restatement of the line
  below it.

A reader who stops at the docstring should be able to *use*
the function correctly. A reader who continues into the body
should be able to *maintain* it. Neither audience has to read
prose that belongs to the other.

### A worked example

Before — long, repetitive, and it restates the body:

```python
def read_ttl_remaining(payload: Any) -> float | None:
    """Read the remaining TTL from the payload.

    This function reads the TTL remaining from the payload.
    It takes a payload and reads the TTL. If the payload has
    no TTL, it returns None. If the payload has a TTL, it
    returns the TTL as a float.
    """
```

After — same facts, each stated once:

```python
def read_ttl_remaining(payload: Any) -> float | None:
    """The request's remaining TTL in seconds, or ``None``.

    ``None`` means the payload carries no usable TTL — the
    caller falls back to the global ``reaper_idle_time``. A TTL
    of ``0`` or below means the deadline already passed; the
    caller clamps it to zero (reclaimable immediately).

    Accepts the wire payload, a stream entry's field map, or
    the decoded ``data`` mapping. A malformed payload yields
    ``None`` rather than raising: the reader is a reaper, and
    a reader that raises takes the consumer down.
    """
```

The second version is shorter *and* says more: the contract
(the return values), the caller's obligation (the fallback,
the clamping), the accepted shapes, and the failure policy.

### Why this is non-negotiable, beyond readability

The codebase outlives the people who wrote it. A docstring
written for the author's own generation becomes, over time,
the only surviving account of a decision whose rationale
lived in a meeting, a chat, or a mind. The discipline is what
keeps that account.

## Scope: new and modified files only

The discipline applies to **new files and modified files**. Pre-existing files that are merely read but not changed are not retroactively documented; the cost of a global sweep is not justified by the benefit. When you touch a file, bring it up to the new bar in the same commit.
