# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Lightweight validation of a kwargs dict against a Tool's
`input_schema` (JSON-Schema shape, V1 subset).

Why a bespoke validator
-----------------------

The framework deliberately avoids pulling in
`jsonschema` as a dependency for this hook. V1 only
needs to enforce the contract that the
`ToolInvoker.pre_invoke_args_extractor` hook relies on:

  - the merged `args` dict has the types the schema
    declares for its declared properties;
  - required fields are present.

V1 does NOT cover:
  - `oneOf` / `anyOf` / `allOf`;
  - nested objects / arrays (the extractor only
    walks the top level — see ADR-013 §2.2);
  - `pattern`, `minimum`, `maximum` (the Tool
    validates these itself);
  - `$ref`.

If a tenant needs full JSON-Schema validation, the
canonical answer is to install `jsonschema` and wrap
this module — the merge logic in `ToolInvoker` is
schema-validator-agnostic.
"""

from __future__ import annotations

from typing import Mapping, Optional

from kntgraph.tools.protocol import ToolArgValue
from kntgraph.tools.schema import walk_schema


class SchemaValidationError(Exception):
    """
    Raised when the merged args do not validate against
    the schema.

    `missing` lists the required fields that were not
    supplied (caller + extractor combined). `unexpected`
    lists keys that are NOT in the schema — passing
    extra kwargs to a Tool is almost always a bug or a
    prompt-injection vector; we drop them (no raise)
    but report them so the caller can log.
    """

    def __init__(
        self,
        *,
        missing: list[str],
        unexpected: list[str],
        type_mismatches: list[tuple[str, str, str]],
    ) -> None:
        self.missing = missing
        self.unexpected = unexpected
        self.type_mismatches = type_mismatches
        bits: list[str] = []
        if missing:
            bits.append(f"missing required: {sorted(missing)}")
        if type_mismatches:
            pretty = ", ".join(
                f"{name}: expected {want}, got {got}"
                for name, want, got in type_mismatches
            )
            bits.append(f"type mismatches: {pretty}")
        msg = "; ".join(bits) if bits else "schema validation failed"
        super().__init__(msg)


def _python_type_name(value: ToolArgValue) -> str:
    """String name of the Python type (intentional simplification)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _matches_type(value: ToolArgValue, json_type: str) -> bool:
    """
    Minimal JSON-Schema → Python type check.

    - `"string"`: str
    - `"integer"`: int (NOT bool — bools are sneaky ints in Python)
    - `"number"`: int or float
    """
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def validate_args(
    args: Mapping[str, ToolArgValue],
    schema: Optional[Mapping[str, ToolArgValue]],
) -> None:
    """
    Validate `args` against `schema` (a JSON-Schema
    object schema). Raises `SchemaValidationError` on
    failure. Returns `None` on success.

    Unknown / extra keys in `args` that are NOT in the
    schema's `properties` are reported in
    `error.unexpected` but do NOT raise on their own —
    the function still raises for missing required or
    type mismatches. The caller decides what to do
    with `unexpected`.
    """
    fields = walk_schema(schema)
    declared = {f.name for f in fields}

    missing, type_mismatches = _collect_required_and_type_errors(args, fields)
    unexpected = _collect_unexpected_keys(args, declared)

    if missing or type_mismatches:
        raise SchemaValidationError(
            missing=missing,
            unexpected=unexpected,
            type_mismatches=type_mismatches,
        )


def _collect_required_and_type_errors(
    args: Mapping[str, ToolArgValue],
    fields: list,
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Walk the schema's required fields and record
    the ones that are missing from ``args`` plus the
    type mismatches (a field is in ``args`` but the
    value does not match the JSON type). Returns
    ``(missing, type_mismatches)`` — both lists in
    the same order as the schema's fields, so the
    error report is deterministic."""
    missing: list[str] = []
    type_mismatches: list[tuple[str, str, str]] = []
    for spec in fields:
        if spec.required and spec.name not in args:
            missing.append(spec.name)
            continue
        if spec.name in args and not _matches_type(args[spec.name], spec.json_type):
            type_mismatches.append(
                (
                    spec.name,
                    spec.json_type,
                    _python_type_name(args[spec.name]),
                )
            )
    return missing, type_mismatches


def _collect_unexpected_keys(
    args: Mapping[str, ToolArgValue],
    declared: set[str],
) -> list[str]:
    """Return the keys in ``args`` that are not in
    the schema's ``declared`` set. The list is in
    insertion order (Python dicts preserve
    insertion order) so the error report matches
    what the caller sent over the wire."""
    return [k for k in args.keys() if k not in declared]


__all__ = ["SchemaValidationError", "validate_args"]
