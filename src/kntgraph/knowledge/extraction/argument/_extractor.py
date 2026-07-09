# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``SchemaArgumentExtractor`` -- the orchestrator.

Walks a Tool's ``input_schema``, finds each scalar field
in the user's text via a ``FieldFinder``, coerces the
raw values to the schema type, and packages the result
as an ``ArgExtraction``.

Iter 28: moved from
``kntgraph.agents.knowledge.argument_extractor._extractor``
to the framework. The class is pure logic (no I/O
beyond the ``FieldFinder`` it composes), so it belongs
with the framework's other argument-extraction pieces.

The class depends on:

  - ``kntgraph.knowledge.extraction.base`` (the
    ``ArgumentExtractor`` Protocol + ``ArgExtraction``).
  - ``kntgraph.tools.registry`` (``ToolRegistry``
    for schema lookup).
  - ``kntgraph.tools.schema`` (``FieldSpec``,
    ``walk_schema``, ``compute_schema_version``).
  - ``kntgraph.knowledge.extraction.argument._finder``
    (``FieldFinder`` Protocol).
  - ``kntgraph.knowledge.extraction.argument._coerce``
    (the ``coerce`` helper).

All of these are now framework modules. The extractor
itself is therefore framework-clean.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Optional

from kntgraph.core.result import ToolError
from kntgraph.infra.config import fresh_settings
from kntgraph.knowledge.extraction.argument._coerce import coerce
from kntgraph.knowledge.extraction.argument._finder import FieldFinder
from kntgraph.knowledge.extraction.base import (
    ArgExtraction,
    ArgumentExtractor,
    ExtractedValue,
)
from kntgraph.tools.schema import (
    compute_schema_version,
    walk_schema,
)

if TYPE_CHECKING:
    from kntgraph.tools.registry import ToolRegistry
    from kntgraph.tools.schema import FieldSpec


class SchemaArgumentExtractor(ArgumentExtractor):
    """
    Walk a Tool's `input_schema`, find each scalar field
    in the user's text via a `FieldFinder`, coerce, and
    package the result as an `ArgExtraction`.

    Construction takes:
      - `registry`: the `ToolRegistry` whose tools'
        schemas are the source of truth.
      - `finder`: a `FieldFinder`. Production uses
        `GlinerFieldFinder`; tests use a fake.
      - `field_threshold`: minimum confidence to keep
        a field. Default from
        `Settings.arg_threshold` (env
        `KNT_ARG_THRESHOLD`), fallback `0.5`.

    The extractor is **stateless** across calls: same
    (text, tool_name, schema_version) -> same result.
    Safe to share across coroutines.
    """

    def __init__(
        self,
        registry: "ToolRegistry",
        finder: FieldFinder,
        *,
        field_threshold: Optional[float] = None,
    ) -> None:
        if registry is None:
            raise ValueError("registry is required")
        if finder is None:
            raise ValueError("finder is required")
        effective_threshold = (
            float(field_threshold)
            if field_threshold is not None
            else float(fresh_settings().arg_threshold)
        )
        if not 0.0 <= effective_threshold <= 1.0:
            raise ValueError(
                f"field_threshold must be in [0, 1], got {effective_threshold!r}"
            )
        self._registry = registry
        self._finder = finder
        self._field_threshold = effective_threshold

    @property
    def field_threshold(self) -> float:
        return self._field_threshold

    async def extract(
        self,
        text: str,
        tool_name: str,
    ) -> ArgExtraction:
        """
        Extract `tool_name`'s arguments from `text`.

        Returns an `ArgExtraction` with `fields={}` for:
          - unregistered `tool_name` (the caller routes
            the error to DLQ);
          - empty / whitespace-only `text`;
          - tools with no scalar properties in the schema.
        """
        tool = self._registry.get(tool_name)
        if tool is None:
            raise ToolError(f"tool {tool_name!r} not registered")
        schema = tool.input_schema
        fields = walk_schema(schema)
        version = compute_schema_version(schema)
        if not text or not text.strip() or not fields:
            return _empty_extraction(tool_name, version)

        started = time.perf_counter()
        results = await asyncio.gather(
            *(self._finder.find(text, f) for f in fields),
            return_exceptions=True,
        )
        _ = time.perf_counter() - started  # measured at the Role level

        return self._collect_results(fields, results, tool_name, version)

    def _collect_results(
        self,
        fields: list[FieldSpec],
        results: list[object],
        tool_name: str,
        version: str,
    ) -> ArgExtraction:
        """Reduce the per-field ``(value, confidence)``
        pairs (or exception / ``None`` placeholders) into
        a final ``ArgExtraction``.

        A field finder raising is not a hard error for
        the whole extract (one bad field shouldn't kill
        the others); the Role-level error path catches
        true extract failures.
        """
        out_fields: dict[str, ExtractedValue] = {}
        out_confs: dict[str, float] = {}
        for spec, res in zip(fields, results):
            if not isinstance(res, tuple):
                # Either ``None`` (no match) or an
                # ``Exception`` (finder raised). Both
                # drop the field.
                continue
            raw_value, confidence = res
            if confidence < self._field_threshold:
                continue
            coerced = coerce(raw_value, spec)
            if coerced is None:
                continue
            out_fields[spec.name] = coerced
            out_confs[spec.name] = confidence
        return ArgExtraction(
            tool_name=tool_name,
            fields=out_fields,
            confidences=out_confs,
            schema_version=version,
        )


def _empty_extraction(tool_name: str, version: str) -> ArgExtraction:
    """Build the empty-fields ``ArgExtraction`` for
    early-exit cases (unregistered tool, empty text,
    no scalar fields). Pulled out of ``extract`` so the
    async orchestrator stays flat.
    """
    return ArgExtraction(
        tool_name=tool_name,
        fields={},
        confidences={},
        schema_version=version,
    )


__all__ = ["SchemaArgumentExtractor"]
