# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pure fingerprint / signature helpers for the Solution tier.

The ``SolutionExtractor`` builds ``Problem`` /
``Action`` / ``Outcome`` values from raw EventLog data;
each carries a stable hash that becomes the ``MERGE``
key in FalkorDB. This module owns the hashing functions
so the algorithm can evolve in one place.
"""

from __future__ import annotations

import json
from typing import Mapping

from kntgraph.core.event import Event
from kntgraph.infra.hashing import short_hash
from kntgraph.agents.memory.solutions._values import JsonValue


def fingerprint_problem(data: Mapping[str, JsonValue]) -> str:
    """
    Compute a stable fingerprint of a tool event's
    `.requested` `data` payload.

    Same input → same fingerprint → same `(:Problem)`
    node. JSON-serialised with `sort_keys=True` so
    reordering dict keys does not change the hash.
    """
    payload = json.dumps(dict(data), sort_keys=True, default=str)
    return short_hash(payload)


def fingerprint_params(data: Mapping[str, JsonValue]) -> str:
    """
    Stable hash of a tool call's parameters for the
    `(:Action).params_fingerprint` key.

    Same algorithm as `fingerprint_problem` (sha256 →
    first 16 hex chars). Kept as a separate function
    so that the two fingerprints can diverge in the
    future (e.g. one normalises whitespace, the other
    does not) without breaking callers that use the
    other.
    """
    return fingerprint_problem(data)


def result_signature(result: JsonValue) -> str:
    """
    Stable hash of a tool result payload.

    `result` may be any JSON-serialisable value. The
    signature lets the promoter dedup equivalent
    completions (same input + same output = same
    Action + same Outcome).
    """
    try:
        payload = json.dumps(result, sort_keys=True, default=str)
    except (TypeError, ValueError):
        # Unserialisable results fall back to the repr.
        # This is rare in practice (tool results are
        # usually dicts) but defends against weird
        # `Result[Set, Err]` shapes.
        payload = repr(result)
    return short_hash(payload)


def params_from_requested(event: Event) -> Mapping[str, JsonValue]:
    """
    Extract the `data` payload of a `tool.*.requested`
    event as a plain dict. Used to build both the
    Problem fingerprint and the Action params.

    The conversion is identity for dict payloads (the
    common case). For non-dict payloads, the value is
    wrapped under the `value` key so the fingerprint is
    well-defined.
    """
    raw = dict(event.data)
    if not raw:
        return {"value": ""}
    return cast_any_to_json(raw)


def _coerce_to_json(v: JsonValue) -> JsonValue:
    """
    Coerce a raw event-payload value into ``JsonValue``.

    Strings, ints, floats, bools, ``None`` and
    dict / list compositions pass through; anything
    else falls back to ``str(v)`` (the same safety net
    ``json.dumps(..., default=str)`` provides
    elsewhere). Kept private — the public fingerprint
    functions use it once, internally.
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _coerce_to_json(vv) for k, vv in v.items()}
    if isinstance(v, list):
        return [_coerce_to_json(x) for x in v]
    return str(v)


def cast_any_to_json(raw: dict[str, JsonValue]) -> Mapping[str, JsonValue]:
    """Convert a raw event payload to ``JsonValue``."""
    return {k: _coerce_to_json(v) for k, v in raw.items()}


def maybe_float(v: JsonValue) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
