# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Value objects for the Solution tier.

Contains the frozen dataclasses that flow through the
SolutionExtractor → SolutionPromotionBus → SolutionPromoter
pipeline:

* :class:`Problem` — the ``(:Problem)`` node (fingerprint +
  tags + text used for embedding).
* :class:`Action` — the ``(:Action)`` node
  (request_event_id + tool + params_fingerprint + params).
* :class:`Outcome` — the ``(:Outcome)`` node (status +
  latency + result_signature + error).
* :class:`SolutionCandidate` — the triple
  ``(Problem, Action, Outcome)`` plus the source agent
  and a starting confidence.
* :class:`PromoteStats` — outcome of a
  ``SolutionPromoter.pump_once`` call.

The ``ToolDescriptor`` lives in
:mod:`kntgraph.agents.tools.descriptors` and is imported
from there when needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Union


# Framework-level type for JSON-serialisable values
# crossing the event / fingerprint boundary. The
# framework treats these as opaque; it only ever
# JSON-serialises them with ``default=str`` as a
# safety net. Concrete event payloads are documented
# in ADR-013 §2.
JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[
    JsonScalar,
    dict[str, "JsonValue"],
    list["JsonValue"],
]


@dataclass(frozen=True, slots=True)
class Problem:
    """
    The "shape" of the situation that triggered a tool
    call. In the FalkorDB schema, this is the `(:Problem)`
    node. The `fingerprint` is the canonical identity
    used in `MERGE`; it is a stable hash of the
    serialised `data` of the tool `.requested` event.
    """

    fingerprint: str
    # Tags extracted from the `.requested` payload (CNPJ,
    # UF, regime_tributario, valor_faixa, etc.). Stored as
    # JSON in `(:Problem).tags_json`. The values are
    # application-defined; the framework only stores
    # them.
    tags: dict[str, str] = field(default_factory=dict)
    # Free-form text used to embed the problem. Defaults
    # to `json.dumps(data, sort_keys=True)`. The embed
    # call is the promoter's responsibility, not the
    # extractor's.
    text: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            raise ValueError("Problem.fingerprint must be non-empty")


@dataclass(frozen=True, slots=True)
class Action:
    """
    A concrete tool call: a `(:Action)` node. The
    `request_event_id` is the EventLog `event_id` of the
    `tool.{name}.requested` event — stable across replays
    (uuid5) and used as the `MERGE` key in FalkorDB.
    """

    request_event_id: str
    tool_name: str
    # Stable hash of the serialised `.requested` data
    # (post-redaction). Used for confidence bump
    # cross-agent: two calls with the same
    # `params_fingerprint` are the same Action shape.
    params_fingerprint: str
    # The parameters as a dict. **ALREADY REDACTED** by
    # the time it reaches the promoter (the promoter is
    # the only thing that sees unredacted data, and it
    # redacts before persisting).
    params: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_event_id:
            raise ValueError("Action.request_event_id must be non-empty")
        if not self.tool_name:
            raise ValueError("Action.tool_name must be non-empty")
        if not self.params_fingerprint:
            raise ValueError("Action.params_fingerprint must be non-empty")


@dataclass(frozen=True, slots=True)
class Outcome:
    """
    The result of a tool call. `(:Outcome)` node,
    anchored to the Action via `(:Action)-[:PRODUCED]`.
    """

    status: str  # "completed" | "failed"
    latency_ms: Optional[float] = None
    # Stable hash of the result payload. Two completions
    # with the same result_signature are observationally
    # equivalent; useful for dedup at the consolidate-time.
    result_signature: str = ""
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed"}:
            raise ValueError(
                f"Outcome.status must be 'completed' or 'failed', got {self.status!r}"
            )


@dataclass(frozen=True, slots=True)
class SolutionCandidate:
    """
    The unit of work that the extractor emits and the
    promoter consumes. A candidate is the triple
    `(Problem, Action, Outcome)` plus the agent that
    produced it and a starting `confidence`.

    `confidence` starts at 1 (every tool call that
    finishes gets +1). The cross-agent bump in
    `SolutionExtractor.bump_confidence` can raise it.
    The `KnowledgeConsolidator` uses `confidence` to
    decide whether the candidate is auto-promoted or
    sent to the review queue (ADR-010 §2.6).
    """

    problem: Problem
    action: Action
    outcome: Outcome
    # The `agent_id` that produced the tool call. Used
    # for the cross-agent bump and for audit logs. The
    # agent is **not** a node in the Solution sub-graph
    # in the MVP (Design A); the agent is recorded in
    # the audit trail (the source events) only.
    source_agent_id: str
    confidence: int = 1
    # The source events for audit. The promoter uses
    # these to record provenance but does NOT write them
    # to the grafo — the EventLog is the audit log.
    source_request_event_id: str = ""
    source_result_event_id: str = ""

    def __post_init__(self) -> None:
        if not self.source_agent_id:
            raise ValueError("SolutionCandidate.source_agent_id must be non-empty")
        if self.confidence < 0:
            raise ValueError(f"confidence must be >= 0, got {self.confidence}")


@dataclass(frozen=True, slots=True)
class PromoteStats:
    """
    Outcome of a `SolutionPromoter.pump_once` call.

    `upserts` counts candidates that landed in FalkorDB.
    `pii_blocked` counts candidates whose PII redaction
    failed (the promoter is fail-closed; these are
    emitted to the EventLog + DLQ by the caller). `failed`
    counts candidates whose I/O (projector) failed.
    `skipped` is reserved for Fase 4 (manual approval
    queue, when a candidate is queued for review rather
    than auto-promoted). Today it is always 0.
    """

    upserts: int = 0
    pii_blocked: int = 0
    skipped: int = 0
    failed: int = 0
    by_tool: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int | dict[str, int]]:
        return {
            "upserts": self.upserts,
            "pii_blocked": self.pii_blocked,
            "skipped": self.skipped,
            "failed": self.failed,
            "by_tool": dict(self.by_tool),
        }
