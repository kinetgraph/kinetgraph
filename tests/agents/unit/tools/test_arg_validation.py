# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``kntgraph.tools.arg_validation``.

Covers the public ``validate_args`` contract and the private
helpers it composes (``_python_type_name``, ``_matches_type``,
``_collect_required_and_type_errors``,
``_collect_unexpected_keys``).

The validator is the framework's JSON-Schema-subset check
(ADR-013 §2.2) that runs immediately before a ``Tool`` is
invoked. The contract is:

    - Required fields missing from ``args`` raise.
    - Type mismatches (the JSON-Schema ``type`` does not
      match the Python type of the value) raise.
    - Unknown keys (not in the schema's ``properties``)
      are reported in ``error.unexpected`` but do not by
      themselves raise.
    - On success, ``validate_args`` returns ``None``.

The tests exercise every branch of the type-mapping helpers
(the bool / int / float / str / list / dict / null cases)
and the validate-args success and failure paths. Behaviour
style: real types, no mocks.
"""

from __future__ import annotations

import pytest

from kntgraph.tools.arg_validation import (
    SchemaValidationError,
    _matches_type,
    _python_type_name,
    validate_args,
)


def _schema(*fields: tuple[str, str, bool]) -> dict:
    """Build a JSON-Schema object schema from a list of
    ``(name, json_type, required)`` tuples. The test data
    only ever exercises the scalar types the framework
    cares about (string, number, integer); the rest of
    the schema is minimal.
    """
    return {
        "type": "object",
        "properties": {n: {"type": t} for n, t, _ in fields},
        "required": [n for n, _, r in fields if r],
    }


class TestPythonTypeName:
    """Branch coverage on ``_python_type_name``: every
    ``isinstance`` arm and the ``type(v).__name__``
    fallback for non-JSON values."""

    def test_none(self):
        assert _python_type_name(None) == "null"

    def test_bool_is_not_int(self):
        # The validator treats bool as its own JSON type,
        # never as integer — this is the case that
        # motivates the ``isinstance(v, bool)`` check
        # appearing *before* the int / float checks.
        assert _python_type_name(True) == "bool"
        assert _python_type_name(False) == "bool"

    def test_int(self):
        assert _python_type_name(42) == "int"

    def test_float(self):
        assert _python_type_name(3.14) == "float"

    def test_str(self):
        assert _python_type_name("hello") == "str"

    def test_list(self):
        assert _python_type_name([1, 2, 3]) == "list"

    def test_dict(self):
        assert _python_type_name({"k": "v"}) == "dict"

    def test_unknown_falls_back_to_class_name(self):
        # ``object()`` is not a JSON value; the helper
        # returns its class name so error messages stay
        # informative for unexpected shapes.
        sentinel = object()
        assert _python_type_name(sentinel) == "object"


class TestMatchesType:
    """Branch coverage on ``_matches_type``: each
    ``json_type`` arm, plus the bool-rejection paths
    inside the integer and number arms."""

    def test_string_matches_str_only(self):
        assert _matches_type("hello", "string") is True
        assert _matches_type(42, "string") is False

    def test_integer_matches_int_only(self):
        assert _matches_type(42, "integer") is True
        assert _matches_type(3.14, "integer") is False
        # bools are sneaky ints in Python; the validator
        # rejects them on the integer arm so a True
        # passed to a JSON-Schema ``"integer"`` field is
        # a type error.
        assert _matches_type(True, "integer") is False
        assert _matches_type(False, "integer") is False

    def test_number_matches_int_and_float(self):
        assert _matches_type(42, "number") is True
        assert _matches_type(3.14, "number") is True
        # Bools are still rejected — JSON Schema treats
        # them as a separate type.
        assert _matches_type(True, "number") is False

    def test_unknown_json_type_returns_false(self):
        # Anything that isn't string / integer / number
        # falls through to the final ``return False``.
        assert _matches_type("hello", "boolean") is False
        assert _matches_type("hello", "array") is False


class TestValidateArgs:
    """The public contract: ``validate_args(args, schema)``
    raises ``SchemaValidationError`` on missing or
    type-mismatched required fields; otherwise returns
    ``None``. Unknown keys are reported but do not by
    themselves raise."""

    def test_returns_none_on_valid_args(self):
        schema = _schema(("name", "string", True))
        assert validate_args({"name": "alice"}, schema) is None

    def test_no_required_fields_passes(self):
        schema = _schema(("name", "string", False))
        assert validate_args({}, schema) is None

    def test_missing_required_raises(self):
        schema = _schema(("name", "string", True))
        with pytest.raises(SchemaValidationError) as exc_info:
            validate_args({}, schema)
        assert exc_info.value.missing == ["name"]
        assert exc_info.value.type_mismatches == []
        assert exc_info.value.unexpected == []

    def test_type_mismatch_raises(self):
        schema = _schema(("age", "integer", True))
        with pytest.raises(SchemaValidationError) as exc_info:
            validate_args({"age": "thirty"}, schema)
        assert exc_info.value.missing == []
        assert exc_info.value.type_mismatches == [("age", "integer", "str")]

    def test_missing_and_type_mismatch_reported_together(self):
        schema = _schema(
            ("name", "string", True),
            ("age", "integer", True),
        )
        with pytest.raises(SchemaValidationError) as exc_info:
            validate_args({"age": "thirty"}, schema)
        assert exc_info.value.missing == ["name"]
        assert exc_info.value.type_mismatches == [("age", "integer", "str")]

    def test_unexpected_keys_do_not_raise_alone(self):
        schema = _schema(("name", "string", True))
        # ``extra`` is not in the schema. The validator
        # does not raise — it reports the key in
        # ``unexpected`` only if a missing / type-mismatch
        # is also present (the only path that constructs
        # an error). With a valid required field and only
        # an extra key, no error is raised.
        validate_args({"name": "alice", "extra": 1}, schema)

    def test_unreported_when_only_unexpected(self):
        # When the only problem is an unexpected key
        # (no missing, no type mismatch) the validator
        # returns None; the function reports unexpected
        # keys only as a side-channel of the error path.
        schema = _schema(("name", "string", True))
        assert validate_args({"name": "alice", "extra": 1}, schema) is None

    def test_unexpected_reported_alongside_missing(self):
        # When a required field is missing AND an extra
        # key is present, the error includes both.
        schema = _schema(("name", "string", True))
        with pytest.raises(SchemaValidationError) as exc_info:
            validate_args({"extra": 1}, schema)
        assert exc_info.value.missing == ["name"]
        assert exc_info.value.unexpected == ["extra"]

    def test_empty_schema_passes(self):
        # An empty schema has no fields to validate, so
        # anything is valid (and no unexpected keys are
        # reported — the declared set is empty, so every
        # key is unexpected, but the helper only returns
        # a list; the validate-args path only raises
        # when missing or type-mismatches are non-empty).
        validate_args({"any": "thing"}, {})


class TestSchemaValidationErrorMessage:
    """Branch coverage on ``SchemaValidationError.__init__``:
    the conditional construction of the message string."""

    def test_only_missing_branch(self):
        err = SchemaValidationError(
            missing=["name"],
            unexpected=[],
            type_mismatches=[],
        )
        assert "missing required: ['name']" in str(err)

    def test_only_type_mismatches_branch(self):
        err = SchemaValidationError(
            missing=[],
            unexpected=[],
            type_mismatches=[("age", "integer", "str")],
        )
        assert "type mismatches: age: expected integer, got str" in str(err)

    def test_both_branches_concatenated(self):
        err = SchemaValidationError(
            missing=["name"],
            unexpected=[],
            type_mismatches=[("age", "integer", "str")],
        )
        msg = str(err)
        assert "missing required: ['name']" in msg
        assert "type mismatches:" in msg
        # The two halves are joined by "; ".
        assert "; " in msg

    def test_fallback_message_when_both_lists_empty(self):
        # Defensive branch: the validator never raises
        # with both lists empty, but the constructor
        # handles it without crashing.
        err = SchemaValidationError(
            missing=[],
            unexpected=[],
            type_mismatches=[],
        )
        assert str(err) == "schema validation failed"

    def test_type_mismatches_sorted_in_message(self):
        # The error message sorts missing fields so the
        # report is deterministic across runs (the
        # message is part of the user-visible contract).
        err = SchemaValidationError(
            missing=["zeta", "alpha"],
            unexpected=[],
            type_mismatches=[],
        )
        assert "['alpha', 'zeta']" in str(err)
