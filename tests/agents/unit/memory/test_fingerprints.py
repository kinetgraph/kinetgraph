# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``kntgraph.agents.memory.solutions._fingerprints``.

The module is a bag of pure functions:

    - ``fingerprint_problem`` / ``fingerprint_params``:
      stable hashes of dict payloads. Used as the
      ``(:Problem)`` node key and the
      ``(:Action).params_fingerprint`` key in FalkorDB.
    - ``result_signature``: stable hash of any
      JSON-serialisable value. The promoter dedups
      completions by (input, output) hash.
    - ``params_from_requested``: extracts the ``data``
      payload of a tool event.
    - ``cast_any_to_json``: recurses through a raw event
      payload, coercing non-JSON values to ``str``.
    - ``maybe_float``: defensive float coercion for
      numeric-looking values that may be ``None`` or
      stringly-typed.

The tests are behaviour-style: real ``Event`` objects,
no mocks, no I/O. The functions are pure, so the
test surface is small.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional

from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.event.event import Event

from kntgraph.agents.memory.solutions._fingerprints import (
    cast_any_to_json,
    fingerprint_params,
    fingerprint_problem,
    maybe_float,
    params_from_requested,
    result_signature,
)


def _ts() -> datetime:
    return datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _event(
    *,
    event_type: str,
    agent_id: str = "agent-1",
    data: Optional[Mapping[str, Any]] = None,
) -> Event:
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data=MappingProxyType(dict(data or {})),
        correlation=CorrelationContext.new(),
        causation_id=None,
        timestamp=_ts(),
    )


class TestFingerprintProblem:
    """``fingerprint_problem`` is the canonical
    ``(:Problem)`` node key. Same input must produce the
    same fingerprint regardless of dict key order."""

    def test_deterministic_for_same_input(self):
        # The two dicts differ only in key insertion
        # order. ``json.dumps(sort_keys=True)`` makes the
        # output identical, so the hash must match.
        a = fingerprint_problem({"x": 1, "y": 2})
        b = fingerprint_problem({"y": 2, "x": 1})
        assert a == b

    def test_different_inputs_produce_different_fingerprints(self):
        a = fingerprint_problem({"x": 1})
        b = fingerprint_problem({"x": 2})
        assert a != b

    def test_empty_dict(self):
        # The function must handle the empty case without
        # raising.
        assert isinstance(fingerprint_problem({}), str)
        assert len(fingerprint_problem({})) > 0

    def test_falls_back_to_str_for_unserialisable(self):
        # ``default=str`` makes ``json.dumps`` succeed
        # even for non-JSON values. The test exercises
        # the safety net: an ``object()`` survives the
        # round-trip.
        class Custom:
            pass

        v = Custom()
        # Should not raise.
        result = fingerprint_problem({"k": v})
        assert isinstance(result, str)


class TestFingerprintParams:
    """``fingerprint_params`` shares its algorithm with
    ``fingerprint_problem`` but is kept as a separate
    function so the two fingerprints can diverge in the
    future. Today they produce the same hash."""

    def test_matches_problem_algorithm(self):
        a = fingerprint_params({"x": 1, "y": 2})
        b = fingerprint_problem({"x": 1, "y": 2})
        assert a == b

    def test_deterministic(self):
        assert fingerprint_params({"k": "v"}) == fingerprint_params({"k": "v"})


class TestResultSignature:
    """``result_signature`` is the ``Outcome``-side hash.
    The ``try / except`` branch fires when the result is
    not JSON-serialisable; the function falls back to
    ``repr(result)`` rather than raising."""

    def test_dict_result(self):
        # The common case: a tool returns a dict.
        sig = result_signature({"text": "hello"})
        assert isinstance(sig, str)
        assert len(sig) > 0

    def test_primitive_result(self):
        # Strings, ints, floats, bools, None — all
        # JSON-serialisable, so the happy path runs.
        for value in ["text", 42, 3.14, True, False, None]:
            assert isinstance(result_signature(value), str)

    def test_unserialisable_falls_back_to_repr(self):
        # ``set`` is not JSON-serialisable. The
        # ``except (TypeError, ValueError)`` branch
        # catches the failure and hashes the repr.
        sig = result_signature({1, 2, 3})
        assert isinstance(sig, str)
        assert len(sig) > 0

    def test_deterministic_for_same_value(self):
        assert result_signature({"a": 1}) == result_signature({"a": 1})


class TestParamsFromRequested:
    """``params_from_requested`` extracts the ``data``
    payload of a tool event. The contract:

    - For a non-empty dict payload, the function walks
      the payload through ``cast_any_to_json`` and
      returns the result.
    - For an empty payload (``{}``), the function
      returns ``{"value": ""}`` so the fingerprint is
      well-defined.
    """

    def test_empty_payload_returns_value_placeholder(self):
        # The branch where ``not raw`` is true.
        ev = _event(event_type="tool.foo.requested", data={})
        assert params_from_requested(ev) == {"value": ""}

    def test_dict_payload_returned_via_cast(self):
        ev = _event(
            event_type="tool.foo.requested",
            data={"text": "hello", "n": 1},
        )
        result = params_from_requested(ev)
        # Identity for JSON-native payloads.
        assert result == {"text": "hello", "n": 1}

    def test_dict_payload_with_non_json_value_coerced(self):
        # A custom object in the payload must come back
        # as ``str(obj)`` thanks to ``cast_any_to_json``.
        class Custom:
            def __str__(self) -> str:
                return "custom-marker"

        ev = _event(
            event_type="tool.foo.requested",
            data={"k": Custom()},
        )
        result = params_from_requested(ev)
        assert result == {"k": "custom-marker"}


class TestCastAnyToJson:
    """``cast_any_to_json`` is a private helper but its
    branches are worth covering: it recurses through
    dict / list / primitive and falls back to ``str``
    for anything it does not recognise."""

    def test_primitives_pass_through(self):
        # The ``isinstance(v, (str, int, float, bool))``
        # branch.
        assert cast_any_to_json({"a": "s"}) == {"a": "s"}
        assert cast_any_to_json({"a": 1}) == {"a": 1}
        assert cast_any_to_json({"a": 1.5}) == {"a": 1.5}
        assert cast_any_to_json({"a": True}) == {"a": True}

    def test_none_passes_through(self):
        # The ``v is None`` arm of the first isinstance
        # check.
        assert cast_any_to_json({"a": None}) == {"a": None}

    def test_dict_recurses(self):
        # Nested dict: every value goes through
        # ``_coerce_to_json`` recursively.
        assert cast_any_to_json({"a": {"b": 1}}) == {"a": {"b": 1}}

    def test_list_recurses(self):
        # The ``isinstance(v, list)`` arm.
        assert cast_any_to_json({"a": [1, 2, 3]}) == {"a": [1, 2, 3]}

    def test_unknown_value_falls_back_to_str(self):
        # A custom object whose ``str`` is informative.
        class Custom:
            def __str__(self) -> str:
                return "tagged"

        assert cast_any_to_json({"a": Custom()}) == {"a": "tagged"}

    def test_nested_dict_keys_coerced_to_str(self):
        # Numeric keys inside a *nested* dict are coerced
        # to ``str`` so the output stays a valid
        # ``Mapping[str, JsonValue]``. Top-level keys are
        # not coerced (the outer dict comprehension
        # passes them through); only the recursive
        # ``_coerce_to_json`` walks dict values and
        # applies the ``str(k)`` conversion there.
        assert cast_any_to_json({"a": {1: "v"}}) == {"a": {"1": "v"}}


class TestMaybeFloat:
    """``maybe_float`` is the defensive float coercion
    used by the Solution extractor when the result
    payload has a numeric-looking field. The function
    must return ``None`` for any value it cannot
    convert, and the conversion path itself has a
    try/except that must be exercised."""

    def test_none_returns_none(self):
        # The ``v is None`` arm.
        assert maybe_float(None) is None

    def test_int_returns_float(self):
        assert maybe_float(42) == 42.0
        assert isinstance(maybe_float(42), float)

    def test_float_returns_float(self):
        assert maybe_float(3.14) == 3.14

    def test_numeric_string_parses(self):
        # The ``float(v)`` succeeds; the ``try`` arm
        # returns the parsed value.
        assert maybe_float("3.14") == 3.14
        assert maybe_float("42") == 42.0

    def test_non_numeric_string_returns_none(self):
        # The ``except (TypeError, ValueError)`` arm.
        assert maybe_float("not a number") is None

    def test_bool_treated_as_numeric(self):
        # bool is a subclass of int in Python; the
        # ``isinstance(v, (str, int, float, bool))`` arm
        # accepts it and the float conversion succeeds.
        assert maybe_float(True) == 1.0
        assert maybe_float(False) == 0.0

    def test_non_primitive_returns_none(self):
        # The final ``return None`` arm: a list is not
        # str/int/float/bool and not None.
        assert maybe_float([1, 2, 3]) is None
        assert maybe_float({"k": "v"}) is None
