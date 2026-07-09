# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.knowledge.extraction.argument -- framework-level
argument-extraction building blocks.

Public surface
--------------

  - :class:`FieldFinder` (Protocol) -- find a value for
    ONE field in text. Implementations return
    ``(value, confidence)`` or ``None``.
  - :class:`RegexFieldFinder` -- pure-logic fallback
    (no I/O, no third-party deps). Useful for known
    formats (CNPJ, CPF, date, money) and tests.
  - :func:`coerce` -- turn a raw ``FieldFinder`` result
    into the JSON-Schema type the Tool expects. Returns
    ``None`` on coercion failure (field is dropped,
    not raised).
  - :class:`SchemaArgumentExtractor` -- the orchestrator.
    Walks a Tool's ``input_schema``, finds each scalar
    field via a ``FieldFinder``, coerces, and packages
    the result as an :class:`ArgExtraction`.
  - :class:`GlinerFieldFinder` -- GLiNER2-backed
    ``FieldFinder``. Eager-loads the model in
    ``__init__``; runs inference in a worker thread
    (``asyncio.to_thread``).

Sub-package layout:

  - ``_finder`` -- ``FieldFinder`` Protocol +
    ``RegexFieldFinder`` (pure).
  - ``_coerce`` -- ``coerce`` helper (pure).
  - ``_extractor`` -- ``SchemaArgumentExtractor``
    orchestrator.
  - ``_gliner_finder`` -- ``GlinerFieldFinder`` and
    the match-extraction helpers
    (:func:`extract_first`, :func:`match_to_value`,
    :func:`field_o`).

Iter 28: this subpackage is the canonical home of
all argument-extraction building blocks. The
``kntgraph.agents.knowledge.argument_extractor`` package is
a re-export shim and will be deleted in a follow-up
commit.

The public facades (``SLMEntityExtractor``,
``SLMIntentClassifier``, ``SLMArgumentExtractor``)
live in :mod:`kntgraph.knowledge.extraction
._slm_facades`. The canonical default adapter
(``GlinerArgumentAdapter``) lives in
:mod:`kntgraph.knowledge.extraction
.gliner_argument`. Both compose the pieces from
this subpackage.
"""

from __future__ import annotations

from ._coerce import CoercedValue, coerce
from ._extractor import SchemaArgumentExtractor
from ._finder import FieldFinder, FieldValue, RegexFieldFinder
from ._gliner_finder import (
    GlinerFieldFinder,
    GlinerMatch,
    GlinerRawResult,
    extract_first,
    field_o,
    match_to_value,
)


__all__ = [
    "CoercedValue",
    "FieldFinder",
    "FieldValue",
    "GlinerFieldFinder",
    "GlinerMatch",
    "GlinerRawResult",
    "RegexFieldFinder",
    "SchemaArgumentExtractor",
    "coerce",
    "extract_first",
    "field_o",
    "match_to_value",
]
