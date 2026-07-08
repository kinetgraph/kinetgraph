# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tool descriptor -- the static description of a Tool.

The canonical :class:`ToolDescriptor` dataclass. Populated
by :meth:`kntgraph.tools.registry.ToolRegistry.list_descriptors`
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
"""

from __future__ import annotations

from dataclasses import dataclass


__all__ = ["ToolDescriptor"]


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str
    description: str
    input_schema_json: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ToolDescriptor.name must be non-empty")
