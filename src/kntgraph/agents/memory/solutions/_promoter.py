# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``SolutionPromoter`` — adapter that drains the
:class:`SolutionPromotionBus`, runs each candidate
through the PII gate (``_promoter_helpers.redact_candidate``),
and delegates to a ``SolutionProjector`` for the actual
FalkorDB ``MERGE``.

The promoter is **fail-closed**: a PII tool failure
(raises or returns ``Err``) OR a projector I/O failure
aborts the candidate. The candidate is counted in the
appropriate bucket (``pii_blocked`` or ``failed``) and
the pump continues with the next one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional, Protocol

from kntgraph.agents.memory.solutions._bus import SolutionPromotionBus
from kntgraph.agents.memory.solutions._promoter_helpers import redact_candidate
from kntgraph.agents.memory.solutions._values import (
    PromoteStats,
    SolutionCandidate,
)

if TYPE_CHECKING:
    from kntgraph.agents.knowledge.solution_projector import SolutionProjector

    class _RedactionResultLike(Protocol):
        """
        Structural type for the redactor's return value.

        The vertical PII implementation
        (``PiiRedactionTool.__call__``) returns a
        ``RedactionResult`` which has a ``.redacted``
        attribute. We use a ``Protocol`` instead of an
        eager import to keep the no-cycle invariant
        documented in Iter 25 (see
        ``_promoter_helpers.py`` for the matching
        declaration used at the call site).
        """

        redacted: object

    # ``Redactor`` is a callable that takes a payload and
    # returns a ``_RedactionResultLike``. The promoter calls
    # it as a function — NOT through the Tool Protocol — so
    # the redactor is decoupled from the Tool orchestration
    # envelope (idempotency_key, Result envelope, event
    # emission). A redactor is a pure transformer; the
    # Tool Protocol is reserved for the orchestrator-facing
    # tools (PiiRedactionTool) that wrap a Redactor.
    #
    # The payload and result types are kept abstract
    # (``object``-shaped) to avoid an eager import of
    # ``kntgraph.agents.tools.pii`` (which would re-introduce
    # the load-time cycle that Iter 25 broke). The
    # vertical PII implementation conforms to this shape
    # by duck typing; the contract surface is the
    # ``.redacted`` attribute.
    #
    # We use the stdlib ``typing.Callable`` (not the
    # framework's ``kntgraph.tools.protocol.Callable``,
    # which is a ``Protocol`` with TypeVars and rejects
    # the ``[X, Y]`` form when assigned to a TypeAlias).
    Redactor = Callable[[object], _RedactionResultLike]


class SolutionPromoter:
    """
    Adapter: drains the `SolutionPromotionBus`, runs
    each `SolutionCandidate` through the PII gate, and
    delegates to the `SolutionProjector` for the actual
    FalkorDB `MERGE`.

    Pipeline per candidate:

      1. PII redaction. The candidate's `problem.text`
         and `action.params` are passed to the
         redactor. The redacted payload REPLACES the
         original in the candidate the projector sees.
         The raw data is never written to FalkorDB.
      2. FalkorDB write. The redacted candidate is
         handed to the `SolutionProjector.upsert` method,
         which emits 4 nodes + 3 edges.
      3. Failure handling. The promoter is fail-closed:
         a PII tool failure (raises or returns `Err`) OR
         a projector I/O failure aborts the candidate.
         The candidate is counted in the appropriate
         bucket (`pii_blocked` or `failed`) and the pump
         continues with the next one.

    Why the PII gate lives here, not in the projector
    ----------------------------------------------------

    The projector (Fase 3.2) is a thin adapter around
    the Cypher. It has no opinion on whether the data
    is sensitive; it persists whatever it is given. The
    promoter owns the PII decision because the PII
    policy is a tenant-level configuration (the
    redactor is pluggable), while the
    projector is a generic adapter. This split also
    keeps the projector testable in isolation (no PII
    tooling required to exercise it).

    Wiring in the consolidator (Fase 2.5) is unchanged
    on the call side: the consolidator constructs
    `SolutionPromoter(tenant_id=..., projector=...,
    redactor=...)` and the consolidator's
    `pump_once` calls `promoter.pump_once(bus)`.

    Iter 25: the redactor slot was previously typed
    as ``Optional[Tool]`` and the framework's
    ``PiiRedactionTool`` was imported eagerly at
    module level. The new design accepts a plain
    ``Callable[[PiiPayload], RedactionResult]``
    (no idempotency_key, no Result envelope). The
    caller (consolidator, integration, example)
    constructs the redactor — typically a
    ``PiiRedactionTool(level=1).redact`` method bound
    to a `Redactor` — and injects it. The promoter
    no longer imports ``PiiRedactionTool`` at
    module level, which breaks the load-time cycle
    documented in ADR-019 §3.2.
    """

    def __init__(
        self,
        *,
        tenant_id: str = "default",
        projector: "SolutionProjector | None" = None,
        redactor: Optional["Redactor"] = None,
        allow_fail_closed: bool = True,
    ) -> None:
        """
        Args:
          tenant_id: the tenant whose graph is written.
          projector: a `SolutionProjector`. When `None`,
            the promoter runs in "skeleton" mode (logs
            and counts; no I/O). This is preserved for
            unit tests and one-shot scripts that want
            to exercise the consolidation loop without
            FalkorDB.
          redactor: a ``Callable[[PiiPayload],
            RedactionResult]``. A pure transformer
            that takes a payload (text or dict) and
            returns a ``RedactionResult`` with the
            redacted data. The ``PiiRedactionTool``
            exposes this shape as its public ``redact``
            method. When ``None``, the promoter runs
            in "no-redact" mode (the payload is passed
            through unchanged). This is the conservative
            default for tests; production deployments
            should inject a real redactor.
          allow_fail_closed: when True (default), an
            exception from the redactor or the
            projector aborts the candidate. When False,
            the exception propagates and the pump
            fails loudly. Production deployments should
            keep the default (True); tests sometimes
            set False to assert error paths.
        """
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        self._tenant_id = tenant_id
        self._projector = projector
        self._redactor = redactor
        self._allow_fail_closed = allow_fail_closed
        # Cumulative stats (process-lifetime). Useful
        # for /metrics scraping and for tests.
        self._stats = PromoteStats()

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def cumulative_stats(self) -> PromoteStats:
        return self._stats

    @property
    def has_projector(self) -> bool:
        """True when a `SolutionProjector` is wired in."""
        return self._projector is not None

    @property
    def has_redactor(self) -> bool:
        """True when a redactor is wired in."""
        return self._redactor is not None

    # ------------------------------------------------------------------ API

    async def upsert_solution(self, candidate: SolutionCandidate) -> int:
        """
        Persist a single candidate.

        Steps:

          1. Redact PII. If redaction fails, raise
             (when `allow_fail_closed=False`) or
             return `0` (when `True`; the caller's
             stats counter handles the bookkeeping).
          2. Delegate to the projector. When no
             projector is wired, log a structured
             record (legacy Fase 2 behaviour) and
             return 1.

        Returns the count of nodes written (`4`) on
        success, `0` on fail-closed.
        """
        import structlog

        log = structlog.get_logger()
        redacted = await redact_candidate(self, candidate)
        if redacted is None:
            return 0
        if self._projector is None:
            log.info(
                "solution.promoter.upsert.skeleton_only",
                tenant_id=self._tenant_id,
                tool=redacted.action.tool_name,
                request_event_id=(redacted.action.request_event_id),
                status=redacted.outcome.status,
                confidence=redacted.confidence,
            )
            return 1
        return await self._projector.upsert(redacted)

    async def pump_once(self, bus: SolutionPromotionBus) -> PromoteStats:
        """
        Drain the bus and persist each candidate.

        Returns the stats for THIS pump (not the
        cumulative stats). Fail-closed: a candidate
        whose PII redaction or projector I/O fails
        is counted in the appropriate bucket and the
        pump continues with the next one.
        """
        import structlog

        log = structlog.get_logger()
        candidates = bus.drain()
        if not candidates:
            return PromoteStats()
        upserts = 0
        pii_blocked = 0
        skipped = 0
        failed = 0
        by_tool: dict[str, int] = {}
        for c in candidates:
            try:
                n = await self.upsert_solution(c)
                if n == 0:
                    # Fail-closed path: PII gate rejected
                    # the candidate. We do not write.
                    pii_blocked += 1
                    log.warning(
                        "solution.promoter.pii_blocked",
                        tenant_id=self._tenant_id,
                        tool=c.action.tool_name,
                        request_event_id=(c.action.request_event_id),
                    )
                else:
                    upserts += 1
                    by_tool[c.action.tool_name] = by_tool.get(c.action.tool_name, 0) + 1
            except Exception as e:  # noqa: BLE001
                if self._allow_fail_closed:
                    failed += 1
                    log.warning(
                        "solution.promoter.upsert_failed",
                        tenant_id=self._tenant_id,
                        tool=c.action.tool_name,
                        request_event_id=(c.action.request_event_id),
                        error=str(e),
                    )
                else:
                    raise
        # Update cumulative stats. Stats is a frozen
        # dataclass; we replace.
        self._stats = PromoteStats(
            upserts=self._stats.upserts + upserts,
            pii_blocked=self._stats.pii_blocked + pii_blocked,
            skipped=self._stats.skipped + skipped,
            failed=self._stats.failed + failed,
            by_tool={
                **self._stats.by_tool,
                **{k: self._stats.by_tool.get(k, 0) + v for k, v in by_tool.items()},
            },
        )
        return PromoteStats(
            upserts=upserts,
            pii_blocked=pii_blocked,
            skipped=skipped,
            failed=failed,
            by_tool=by_tool,
        )
