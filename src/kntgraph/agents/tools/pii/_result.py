# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Return shape for ``PiiRedactionTool.redact``.
"""

from __future__ import annotations

from dataclasses import dataclass

from kntgraph.agents.tools.pii._patterns import PiiPayload


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """
    The outcome of a `redact` call.

    `redacted` is the new payload (with PII replaced by
    `<PII:kind>` placeholders). `counts` is the
    `kind → count` map of how many PII tokens were
    replaced. `level` echoes the level that was applied.
    """

    redacted: PiiPayload
    counts: dict[str, int]
    level: int
