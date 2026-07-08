# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``PiiRedactionTool`` — the orchestrator.

Implements the ``Tool`` Protocol; coordinates the
level-1 (regex) and level-2 (NER) redaction passes.
Holds configuration (level, labels, optional extractor)
and emits a ``RedactionResult``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.infra.config import fresh_settings
from kntgraph.agents.tools.protocol import ToolArgValue
from kntgraph.agents.tools.pii._level1 import redact_value
from kntgraph.agents.tools.pii._level2 import ner_redact
from kntgraph.agents.tools.pii._patterns import DEFAULT_PII_LABELS, PiiPayload
from kntgraph.agents.tools.pii._result import RedactionResult
from kntgraph.agents.tools.protocol import Tool

if TYPE_CHECKING:
    from kntgraph.knowledge.extraction.base import EntityExtractor


class PiiRedactionTool(Tool):
    """
    Redact PII from a payload before it is persisted.

    Implements the `Tool` Protocol. The single capability
    is `redact(payload)`; the `Tool` Protocol's
    `invoke` method delegates to it. The Tool is
    self-contained: it does not depend on Redis, FalkorDB
    or any external service for level 1. Levels 2/3
    delegate to an `EntityExtractor` (heuristic or
    GLiNER2-based) which is passed in by the caller.
    """

    def __init__(
        self,
        *,
        level: Optional[int] = None,
        entity_extractor: "Optional[EntityExtractor]" = None,
        labels: tuple[str, ...] = DEFAULT_PII_LABELS,
    ) -> None:
        """
        Args:
          level: 1, 2 or 3. Default from
            `Settings.pii_level` (env
            `FMH_PII_LEVEL`), fallback `1`. Levels 2/3
            require `entity_extractor` (or the
            construction raises).
          entity_extractor: an `EntityExtractor`
            (`knowledge.extraction.base`) used at
            level 2. Optional. The heuristic extractor
            is a fine default for level 2; GLiNER2
            subclass is the high-grade option.
          labels: label set for level 2/3. Default
            `DEFAULT_PII_LABELS`. Override per tenant
            via the consolidator's wiring (Fase 4).
        """
        env_level_raw = fresh_settings().pii_level
        if level is not None:
            self._level = int(level)
        elif env_level_raw is not None and env_level_raw >= 0:
            self._level = int(env_level_raw)
        else:
            self._level = 1
        if self._level not in (1, 2, 3):
            raise ValueError(f"PII level must be 1, 2 or 3, got {self._level}")
        if not labels:
            raise ValueError("labels must be non-empty")
        self._labels = labels
        self._entity_extractor = entity_extractor
        if self._level in (2, 3) and self._entity_extractor is None:
            # Heuristic is a safe default for level 2.
            # The constructor imports lazily to avoid
            # pulling the heuristic module when the
            # caller only uses level 1.
            from kntgraph.knowledge.extraction.heuristic import (
                HeuristicEntityExtractor,
            )

            self._entity_extractor = HeuristicEntityExtractor()

    # ------------------------------------------------------------------ Tool Protocol

    name: str = "fmh.pii.redact"
    description: str = (
        "Redact PII (CPF, CNPJ, e-mail, telefone, etc.) from a "
        "payload before persistence. Returns a `RedactionResult` "
        "in a `Result`. Fail-closed: any error propagates as "
        "`Err(ToolError)` so the caller must NOT persist."
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "payload": {
                "description": (
                    "The payload to redact. May be a dict, list or "
                    "scalar; recursion is supported."
                ),
            },
        },
        "required": ["payload"],
    }

    async def invoke(
        self,
        *,
        idempotency_key: str,
        payload: PiiPayload,
        **kwargs: "ToolArgValue",
    ) -> Result[RedactionResult, ToolError]:
        """
        Tool Protocol entry point. Delegates to `redact`.

        `idempotency_key` is required by the Protocol but
        not used (the redaction is itself idempotent
        per payload). The argument is accepted so the
        tool can be invoked via the standard
        `ToolInvoker`.
        """
        try:
            result = await self.redact(payload)
        except Exception as e:  # noqa: BLE001
            # Fail-closed: never raise, but surface the
            # error as `Err(ToolError)`. The caller
            # (SolutionPromoter) inspects the result
            # and decides whether to drop the candidate.
            return Err(ToolError(f"pii_redact_failed: {e!r}"))
        return Ok(result)

    # ------------------------------------------------------------------ public

    async def redact(self, payload: PiiPayload) -> RedactionResult:
        """
        Redact PII from `payload`.

        Level 1 runs first; if `level >= 2`, the
        `EntityExtractor` is run on the redacted text
        and any entities whose type is in
        `self._labels` are replaced by their canonical
        form (`<PII:org>` for `org`, etc.). The
        extractor is a *complement* to the regex, not
        a replacement.

        Level 3 is documented in the ADR as a
        GLiNER2-v1.5-with-`task="pii"` audit pass that
        runs in batch. In the MVP the level 3 path is
        identical to level 2 (the audit batcher is a
        follow-up; the level flag is accepted for
        configuration symmetry).
        """
        counts: dict[str, int] = {}
        # Level 1: regex.
        redacted = redact_value(payload, counts)
        # Level 2: NER extractor.
        if self._level >= 2 and self._entity_extractor is not None:
            await ner_redact(
                redacted,
                counts,
                entity_extractor=self._entity_extractor,
                labels=self._labels,
            )
        # Level 3: same engine, different task. Today
        # the framework runs the same extractor; a
        # future follow-up swaps in a v1.5
        # `task="pii"` model.
        if self._level >= 3 and self._entity_extractor is not None:
            # Idempotent: nothing extra to do beyond
            # the level-2 pass. The level flag is
            # preserved on the result for audit.
            pass
        return RedactionResult(redacted=redacted, counts=counts, level=self._level)
