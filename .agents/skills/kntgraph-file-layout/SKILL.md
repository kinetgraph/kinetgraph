---
name: kntgraph-file-layout
description: Use when splitting a kntgraph module that exceeds 500 lines, when naming a private sub-module, or when reviewing the public/private boundary of a kntgraph package. Covers the 500-line guideline, the _private.py prefix convention, the public __init__.py __all__ contract, and the F401 / SLF001 lint expectations. Trigger keywords: 500 lines, split module, sub-module, _private.py, __all__, private module, public API, F401, SLF001.
---

# File size and module layout

## 3.1 500-line guideline

Files > 500 lines should be split into sub-modules. The split is **private** (prefix `_private.py`); the public `__init__.py` re-exports the API.

## 3.3 Private modules are private

A module whose name starts with `_` (e.g. `core/event/_codec.py`) is **internal to its parent package**. External code MUST NOT import from private modules.

The linter catches some of the symptoms:

- `F401` — unused imports
- `SLF001` — private attribute access

The gate that enforces the public API is the `__all__ = [...]` declaration in the `__init__.py`.
