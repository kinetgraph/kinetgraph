# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Internal PII-gate logic for :class:`SolutionPromoter`.

``_redact_candidate`` runs each candidate through
the redactor (a ``Callable[[PiiPayload],
RedactionResult]``) and returns a NEW
``SolutionCandidate`` with the redacted data, or
``None`` when the redaction fails (fail-closed).

Iter 25: the redactor is now a plain ``Callable``,
not a ``Tool``. The previous design called
``redactor.invoke(idempotency_key=..., payload=...)``
and unwrapped a ``Result[RedactionResult, ToolError]``.
The new design calls the callable directly with
``redactor(payload)`` and expects a
``RedactionResult`` directly. Failures surface as
exceptions (caught by the promoter's fail-closed
path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, cast

from kntgraph.agents.memory.solutions._values import (
    Action,
    JsonValue,
    Problem,
    SolutionCandidate,
)

if TYPE_CHECKING:
    from kntgraph.agents.memory.solutions._promoter import (
        Redactor,
        _RedactionResultLike,
    )
    from kntgraph.agents.memory.solutions._promoter import SolutionPromoter


async def redact_candidate(
    promoter_self: "SolutionPromoter",
    candidate: SolutionCandidate,
) -> Optional[SolutionCandidate]:
    """
    Run PII redaction on the candidate's text and
    params. Returns a NEW candidate with the
    redacted data, or `None` when the redaction
    fails (fail-closed).

    Iter 25: the redactor is a ``Callable`` —
    ``await redactor(payload) -> RedactionResult``.
    Two calls per candidate (problem text + action
    params). The redactor's natural idempotency
    (same input → same output) is sufficient;
    no idempotency_key is needed at this layer.
    """
    redactor_opt: "Redactor | None" = promoter_self._redactor
    if redactor_opt is None:
        # No redactor wired in: pass-through. The
        # candidate is returned with the original
        # (unredacted) data. The promoter is in
        # "no-redact" mode.
        return candidate
    redactor: "Redactor" = redactor_opt

    # Two calls (problem text, action params). The
    # payloads are different but the redaction is
    # naturally idempotent (same input → same output),
    # so no idempotency key is required at this layer.
    #
    # The redactor is duck-typed: it returns an object
    # with a ``.redacted`` attribute (the redacted
    # payload). The vertical PII implementation
    # ``PiiRedactionTool.__call__`` returns a
    # ``RedactionResult`` which satisfies this contract.
    problem_redaction: _RedactionResultLike = await redactor(candidate.problem.text)
    params_redaction: _RedactionResultLike = await redactor(
        dict(candidate.action.params)
    )

    # Build a new candidate with the redacted
    # data. The original is immutable; we
    # construct a fresh `SolutionCandidate`.
    new_problem = Problem(
        fingerprint=candidate.problem.fingerprint,
        tags=candidate.problem.tags,
        text=str(problem_redaction.redacted),
    )
    # The redactor may have returned a non-dict
    # (it walked a string, so the result is the
    # redacted string). We coerce back to dict
    # for the params field.
    new_params: JsonValue = cast(JsonValue, params_redaction.redacted)
    if not isinstance(new_params, dict):
        new_params = {"value": str(new_params)}
    new_action = Action(
        request_event_id=candidate.action.request_event_id,
        tool_name=candidate.action.tool_name,
        params_fingerprint=candidate.action.params_fingerprint,
        params=new_params,
    )
    return SolutionCandidate(
        problem=new_problem,
        action=new_action,
        outcome=candidate.outcome,
        source_agent_id=candidate.source_agent_id,
        confidence=candidate.confidence,
        source_request_event_id=candidate.source_request_event_id,
        source_result_event_id=candidate.source_result_event_id,
    )
