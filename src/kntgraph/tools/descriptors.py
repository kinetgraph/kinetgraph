# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tool descriptor -- the static description of a Tool.

The canonical :class:`ToolDescriptor` dataclass. Populated
by :meth:`kntgraph.tools.manager.WorkerManager.list_descriptors`
and consumed by the
:class:`kntgraph.agents.memory.solutions.SolutionPromoter` on
boot to ``MERGE`` a ``(:Tool)`` node per known tool.
This is the **class** -- the runtime
``(:Action)-[:ON_TOOL]->(:Tool)`` edge points here.

Why a single concrete dataclass (not a Protocol)
------------------------------------------------

The shape is canonical: any Tool description in the
FMH stack carries a ``name``, a human-readable
``description``, and a serialised
``input_schema_json``. There is no vertical variant
today (no ``RichToolDescriptor``, no per-tenant
metadata), so a Protocol would just describe the
dataclass back to itself.

Iter 25: moved from ``kntgraph.agents.tools.descriptors``
to the framework so that ``kntgraph.modules`` can
depend on the canonical home.

v0.18 (ADR-066): the ``_schema_to_json`` helper moved
here from ``tools/registry.py`` (removed in the same
release). The helper is a pure serialisation utility
tied to the descriptor shape, so it lives alongside
the dataclass it produces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

import structlog

__all__ = ["ToolDescriptor", "schema_to_json"]


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str
    description: str
    input_schema_json: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ToolDescriptor.name must be non-empty")


def schema_to_json(schema: "Mapping[str, Any] | None") -> "str | None":
    """Serialise a Tool's ``input_schema`` to a JSON string
    suitable for storage in FalkorDB or for an HTTP
    ``ToolDescriptor`` response.

    Returns ``"{}"`` when ``schema`` is ``None``, ``None``
    when the schema is not serialisable or not round-
    trippable (the caller skips the descriptor in both
    cases; a ``None`` return is the signal to drop the
    tool from the descriptor list).
    """
    log = structlog.get_logger()
    if schema is None:
        return "{}"
    try:
        serialised = json.dumps(schema, sort_keys=True, default=str)
    except (TypeError, ValueError) as e:
        log.warning(
            "tool_registry.schema_not_serialisable",
            error=str(e),
            schema_type=type(schema).__name__,
        )
        return None
    if "<" in serialised and "object at 0x" in serialised:
        log.warning(
            "tool_registry.schema_default_repr_used",
            schema_type=type(schema).__name__,
            note=(
                "Schema contained unrecognised types; "
                "json.dumps fell back to repr. Skipping "
                "this tool's descriptor."
            ),
        )
        return None
    try:
        json.loads(serialised)
    except (TypeError, ValueError) as e:
        log.warning(
            "tool_registry.schema_not_round_trippable",
            error=str(e),
            schema_type=type(schema).__name__,
        )
        return None
    return serialised
