# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``GlinerArgumentAdapter`` -- the GLiNER2-backed
``ArgumentExtractor`` (canonical, framework-level).

Iter 27: moved from
``kntgraph.agents.knowledge.argument_extractor._gliner_wrapper``
to the framework (``kntgraph.knowledge.extraction``).
The vertical path is now a re-export shim.

Why framework-level
-------------------

The adapter is the **canonical default implementation**
of the ``SLMArgumentExtractor`` facade (the public
surface). The facade is framework-level, so its
default backing should live in the framework too --
otherwise the facade has to reach into the vertical
at construction time, which violated AGENTS.md §1.2
("framework never depends on vertical").

The adapter itself is a thin wrapper that combines:

  - :class:`GlinerFieldFinder` (the GLiNER2-backed
    ``FieldFinder``) -- low-level, lazy ``gliner2``
    import.
  - :class:`SchemaArgumentExtractor` (the orchestrator
    that walks a Tool's ``input_schema`` and
    aggregates the field-level finds) -- pure logic,
    no third-party deps.

Both of these pieces used to live in
``kntgraph.agents.knowledge.argument_extractor`` (a
vertical package). They are **moved as-is** to the
framework; the vertical package now re-exports the
canonical implementations for backward compat.

Construction
------------

The constructor of ``GlinerArgumentAdapter`` is the
**only place** in the framework that eagerly imports
the GLiNER2 model. To keep the framework's import
graph clean, the constructor does **local imports**
of the low-level pieces (the field finder and the
orchestrator). This means:

  - ``from kntgraph.knowledge.extraction import
    GlinerArgumentAdapter`` does NOT load ``gliner2``
    or the schema extractor.
  - The import side effects fire only when the
    adapter is *constructed* (which happens on the
    first ``SLMArgumentExtractor()`` with no
    explicit ``adapter=``).
  - Test fakes that pass their own
    ``adapter=`` to ``SLMArgumentExtractor`` never
    trigger the GLiNER2 model load.

Why the lazy import is safe
---------------------------

The lazy import pattern was already established in
the codebase for opt-in third-party deps:

  - ``kntgraph.knowledge.extraction.gliner`` uses
    ``require_optional("gliner2", ...)``.
  - ``kntgraph.knowledge.extraction.gliner_intent``
    uses the same pattern.
  - ``kntgraph.agents.tools.llm`` uses a
    ``TYPE_CHECKING`` + lazy guard for ``litellm``.

The ``GlinerFieldFinder`` and ``SchemaArgumentExtractor``
are NOT opt-in (they are core framework pieces); they
live in the vertical ``argument_extractor`` package
because that was the historical home before Iter 27.
The lazy import here is a *transitional* shim that
preserves the dependency graph while we plan a
follow-up iter to move the underlying pieces too.

Iter 28+ (roadmap): move ``GlinerFieldFinder`` and
``SchemaArgumentExtractor`` themselves into
``kntgraph.knowledge.extraction`` and drop the
vertical ``argument_extractor`` package entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from kntgraph.knowledge.extraction.argument._extractor import (
    SchemaArgumentExtractor,
)
from kntgraph.knowledge.extraction.argument._gliner_finder import (
    GlinerFieldFinder,
)
from kntgraph.knowledge.extraction.base import (
    ArgExtraction,
    ArgumentExtractor,
)


if TYPE_CHECKING:
    from kntgraph.tools.registry import ToolRegistry


class GlinerArgumentAdapter(ArgumentExtractor):
    """
    GLiNER2-backed ``ArgumentExtractor`` (low-level).

    Combines :class:`GlinerFieldFinder` (the GLiNER2
    model wrapper) with :class:`SchemaArgumentExtractor`
    (the orchestrator that walks a Tool's schema and
    aggregates the field-level finds). The
    combination is the production default for
    :class:`SLMArgumentExtractor`.

    The adapter's construction has **two side
    effects**:

      1. The :class:`GlinerFieldFinder` does an eager
         import of ``gliner2`` and loads the model
         checkpoint (delegated to ``require_optional``).
         If the ``gliner2`` package is missing,
         construction fails with a clear error
         pointing to ``kntgraph[gliner]``.
      2. The :class:`SchemaArgumentExtractor` reads
         ``Settings.arg_threshold`` (env
         ``KNT_ARG_THRESHOLD``) for the field
         confidence floor.

    Iter 28: the framework's own
    :mod:`kntgraph.knowledge.extraction.argument`
    subpackage now owns the field-finder and
    orchestrator. The adapter uses **eager imports**
    of these framework modules -- no more lazy
    fallback to ``kntgraph.agents.knowledge
    .argument_extractor``. The framework has 0
    imports `kntgraph -> kntgraph.agents` in any form.
    """

    def __init__(
        self,
        registry: "ToolRegistry",
        *,
        model_name: str | None = None,
        device: Optional[str] = None,
        field_threshold: float = 0.5,
    ) -> None:
        # Iter 28: eager imports. The two pieces we
        # depend on are framework modules now. The
        # adapter is constructed by callers that
        # need GLiNER2 (e.g. the default
        # SLMArgumentExtractor path); the imports fire
        # on construction, which is acceptable
        # because the construction also loads the
        # model (the heavy operation).
        model_name = self._resolve_model_name(model_name)
        finder = GlinerFieldFinder(model_name=model_name, device=device)
        self._inner = SchemaArgumentExtractor(
            registry,
            finder,
            field_threshold=field_threshold,
        )

    @staticmethod
    def _resolve_model_name(model_name: "str | None") -> str:
        """
        Resolve the effective model name from explicit
        arg + Settings.

        The sentinel ``None`` means "no override; use
        Settings". Any explicit value wins. Encapsulated
        so the ``__init__`` body stays flat (CC <= 2) and
        the defaults are easy to test in isolation.
        """
        if model_name is not None:
            return model_name
        from kntgraph.infra.config import fresh_settings

        return fresh_settings().arg_extractor_model_id

    @property
    def model_name(self) -> str:
        return self._inner._finder.model_name  # type: ignore[attr-defined]

    async def extract(self, text: str, tool_name: str) -> ArgExtraction:
        return await self._inner.extract(text, tool_name)


__all__ = ["GlinerArgumentAdapter"]
