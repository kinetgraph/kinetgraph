---
name: kntgraph-style
description: Use when choosing identifiers, writing docstrings, or commenting kntgraph code. Covers the English-prose rule, the domain-driven identifier convention (English for natural concepts, PT-BR-derived names where the domain uses them: ContinuityManager, CNPJ, PIX), and the SQLite-style documentation discipline (every function and every variable documented, concisely, for people not yet born). Trigger keywords: docstring, comment, identifier, naming convention, prose, English, Portuguese, document variables, SQLite-style documentation.
---

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

## Scope: new and modified files only

The discipline applies to **new files and modified files**. Pre-existing files that are merely read but not changed are not retroactively documented; the cost of a global sweep is not justified by the benefit. When you touch a file, bring it up to the new bar in the same commit.
