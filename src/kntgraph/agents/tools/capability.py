# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Capability — a Tool that a Role injects as its I/O surface.

The Capability Protocol mirrors the Tool Protocol exactly.
It exists as a separate type for one reason: it documents
the Role's **intent**. A parameter named
``inference: Capability`` says "this Role needs a Tool-shaped
object for inference"; a parameter named ``io: Capability``
says "this Role needs a Tool-shaped object for external I/O".

There is no sub-typing here (no InferenceCapability vs
IOCapability) and no bridge. Any Tool implementation
satisfies Capability automatically, because the shape
is the same. The distinction is naming, not structure.

The Capability Protocol is the **minimum viable specialisation**:
when the framework grows to the point where inference and
I/O are structurally different at the type level (e.g.
multiple inference backends with materially different
kwargs), the Capability Protocol gains sub-Protocols.
Today, with one inference backend (LiteLLMTool) and a
handful of I/O backends, the naming is enough.

See ADR-006 for the Role contract and AGENTS.md §3.3 for
the naming convention.
"""

from __future__ import annotations

from typing import ParamSpec, Protocol, TypeVar, runtime_checkable

from kntgraph.core.result import Result, ToolError


# Reuse the Tool-level TypeVars: ``Capability`` is a
# semantic alias for ``Tool``, so the success type
# and the kwargs shape are the same.
R = TypeVar("R")
P = ParamSpec("P")


@runtime_checkable
class Capability(Protocol[R]):
    """Same shape as Tool. Different name, by intent."""

    name: str
    description: str
    input_schema: dict

    async def invoke(
        self,
        *,
        idempotency_key: str,
        **kwargs: P.kwargs,
    ) -> Result[R, ToolError]: ...


__all__ = ["Capability"]
