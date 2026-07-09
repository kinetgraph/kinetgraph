# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the framework-level ``tools.schema``
module (Iter 25).

``walk_schema`` and ``FieldSpec`` are pure JSON-Schema
view helpers. They were previously in
``kntgraph.agents.knowledge.argument_extractor._schema`` (a
vertical package). Iter 25 moves them to the framework
because:

  - They are pure functions with zero third-party deps
    (only stdlib ``hashlib`` and ``dataclasses``).
  - The framework's ``arg_validation`` module (in
    ``kntgraph.agents.tools.arg_validation``) is the only
    consumer outside the original package.
  - The cycle that triggered this refactor is rooted
    in ``kntgraph.agents.tools.arg_validation`` reaching
    into the vertical ``kntgraph.agents.knowledge`` package
    for ``walk_schema``.

After Iter 25, ``walk_schema`` lives at
``kntgraph.tools.schema`` (the canonical home) and
the legacy path is a re-export for backward compat.
"""

from __future__ import annotations

import pytest

from kntgraph.tools.schema import (
    FieldSpec,
    compute_schema_version,
    walk_schema,
)


class TestWalkSchema:
    def test_none_schema(self):
        assert walk_schema(None) == []

    def test_empty_schema(self):
        assert walk_schema({}) == []

    def test_no_properties(self):
        assert walk_schema({"type": "object"}) == []

    def test_string_field_required(self):
        result = walk_schema(
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }
        )
        assert result == [FieldSpec(name="name", json_type="string", required=True)]

    def test_integer_field_optional(self):
        result = walk_schema(
            {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
            }
        )
        assert result == [FieldSpec(name="count", json_type="integer", required=False)]

    def test_number_field_with_format(self):
        result = walk_schema(
            {
                "type": "object",
                "properties": {"price": {"type": "number", "format": "currency"}},
            }
        )
        assert result == [
            FieldSpec(
                name="price",
                json_type="number",
                required=False,
                format="currency",
            )
        ]

    def test_unsupported_types_are_skipped(self):
        """Booleans, arrays, objects, nulls are skipped
        by design (V1 only handles scalars)."""
        result = walk_schema(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "is_active": {"type": "boolean"},
                    "tags": {"type": "array"},
                    "metadata": {"type": "object"},
                    "deleted_at": {"type": "null"},
                },
            }
        )
        assert len(result) == 1
        assert result[0].name == "name"

    def test_malformed_property_is_skipped(self):
        result = walk_schema(
            {
                "type": "object",
                "properties": {
                    "good": {"type": "string"},
                    "bad": "not-a-dict",
                },
            }
        )
        assert len(result) == 1
        assert result[0].name == "good"

    def test_required_defaults_tuple(self):
        """``required_defaults`` provides a fallback
        list of required fields when the schema has no
        ``required`` key."""
        result = walk_schema(
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
            },
            required_defaults=("x",),
        )
        assert result == [FieldSpec(name="x", json_type="string", required=True)]


class TestFieldSpec:
    def test_frozen(self):
        spec = FieldSpec(name="x", json_type="string", required=True)
        with pytest.raises(Exception):
            spec.name = "y"  # type: ignore[misc]

    def test_format_optional(self):
        spec = FieldSpec(name="x", json_type="string", required=False)
        assert spec.format is None


class TestComputeSchemaVersion:
    def test_none_schema(self):
        v = compute_schema_version(None)
        assert isinstance(v, str)
        assert len(v) == 16

    def test_stable_across_reorder(self):
        """Two schemas with same properties in different
        order must produce the same version (cache key
        invariant)."""
        s1 = {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "integer"},
            },
            "required": ["a"],
        }
        s2 = {
            "type": "object",
            "properties": {
                "b": {"type": "integer"},
                "a": {"type": "string"},
            },
            "required": ["a"],
        }
        assert compute_schema_version(s1) == compute_schema_version(s2)

    def test_different_required_produces_different_version(self):
        s1 = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
        }
        s2 = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
        }
        assert compute_schema_version(s1) != compute_schema_version(s2)


class TestVerticalDeleted:
    """The vertical ``argument_extractor`` package
    (re-export shim) was deleted in Iter 28 follow-up.
    Its only public symbols (``walk_schema``,
    ``FieldSpec``, ``compute_schema_version``) now
    live exclusively in ``kntgraph.tools.schema``.
    """

    def test_vertical_package_does_not_exist(self):
        """``kntgraph.tools.schema`` is the sole
        canonical home of these helpers."""
        with __import__("pytest").raises((ModuleNotFoundError, ImportError)):
            import kntgraph.agents.knowledge.argument_extractor  # noqa: F401  # pyright: ignore[reportMissingImports]
