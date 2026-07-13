# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.tools.schema -- framework-level JSON-Schema
view helpers.

Holds the dataclass that represents a single scalar field
(``FieldSpec``), the function that walks a JSON-Schema
object (``walk_schema``), and the stable hash function
used as the cache key (``compute_schema_version``).

These helpers are pure (zero third-party deps, only
stdlib ``hashlib`` and ``dataclasses``) and were
previously in
``kntgraph.agents.knowledge.argument_extractor._schema`` (a
vertical package). Iter 25 moves them to the framework
because:

  - The framework's ``arg_validation`` module (in
    ``kntgraph.agents.tools.arg_validation``) is the only
    consumer outside the original package.
  - The cycle that triggered this refactor is rooted
    in ``kntgraph.agents.tools.arg_validation`` reaching
    into the vertical ``kntgraph.agents.knowledge`` package
    for ``walk_schema``.
  - The helpers describe a JSON-Schema view — a
    framework primitive — and have no
    knowledge-package concerns.

The legacy path remains a re-export
(``kntgraph.agents.knowledge.argument_extractor``) for
backward compatibility with consumers that already
import the helpers from the vertical package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from ..core._typing import JsonValue
from ..infra.hashing import short_hash


__all__ = [
    "FieldSpec",
    "compute_schema_version",
    "walk_schema",
]


@dataclass(frozen=True)
class FieldSpec:
    """
    One scalar field to extract from the user's text.

    `name` is the JSON-Schema property name (used as the
    kwargs key when invoking the Tool).

    `json_type` is one of `"string"`, `"number"`,
    `"integer"`. Other types (`"boolean"`, `"array"`,
    `"object"`) are skipped by the extractor (ADR-013
    §2.2 — V1 only handles scalars).

    `required` mirrors JSON-Schema's `required` array
    membership. The extractor does NOT raise on missing
    required fields (the Tool does, after merge); it
    just reports what it found.

    `format` is the optional JSON-Schema `format`
    (e.g. `"date"`, `"date-time"`, `"email"`). Used by
    the coercier to validate or normalise the raw
    value the field finder returned. Unknown formats
    are ignored.
    """

    name: str
    json_type: str  # "string" | "number" | "integer"
    required: bool
    format: Optional[str] = None


def walk_schema(
    schema: Optional[Mapping[str, JsonValue]],
    *,
    required_defaults: tuple[str, ...] = (),
) -> list[FieldSpec]:
    """
    Extract a flat list of `FieldSpec` from a JSON-Schema
    object schema.

    Only the top-level `properties` are walked — V1
    does not handle nested objects or arrays (ADR-013
    §3 known limitation).

    Returns the empty list for `None`, `{}`, or
    malformed schemas. The extractor then returns an
    `ArgExtraction` with `fields={}` and the ToolInvoker
    is expected to invoke with whatever the caller
    already supplied.
    """
    properties = _extract_properties(schema)
    if not properties:
        return []
    required = set(schema.get("required") or required_defaults)  # type: ignore[union-attr]
    return [
        spec
        for name, prop in properties.items()
        if (spec := _field_spec(name, prop, name in required)) is not None
    ]


def _extract_properties(
    schema: Optional[Mapping[str, JsonValue]],
) -> dict[str, JsonValue]:
    """Return the top-level ``properties`` mapping, or
    ``{}`` for any malformed / missing input. Centralises
    the shape guards that previously inlined the
    walker's early-returns.
    """
    if not schema or not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return {}
    return properties


def _field_spec(name: object, prop: object, required: bool) -> Optional[FieldSpec]:
    """Build a ``FieldSpec`` for one property, or
    return ``None`` when the property should be
    skipped (V1 limitation: only string/number/integer).

    Non-string ``format`` values are normalised to
    ``None`` to keep the field shape valid.
    """
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(prop, dict):
        return None
    json_type = prop.get("type")
    if json_type not in ("string", "number", "integer"):
        return None
    fmt = prop.get("format")
    if fmt is not None and not isinstance(fmt, str):
        fmt = None
    return FieldSpec(
        name=name,
        json_type=json_type,
        required=required,
        format=fmt,
    )


def compute_schema_version(schema: Optional[Mapping[str, JsonValue]]) -> str:
    """
    Stable hash of the relevant parts of the schema.

    Used as a cache key suffix. Two schemas with the
    same `properties` and `required` produce the same
    version, regardless of unrelated metadata.
    """
    if not schema or not isinstance(schema, dict):
        return short_hash(b"")
    required_raw = schema.get("required") or []
    if not isinstance(required_raw, list):
        required_raw = []
    properties_raw = schema.get("properties") or {}
    if not isinstance(properties_raw, dict):
        properties_raw = {}
    payload = {
        "properties": properties_raw,
        "required": sorted(required_raw),  # type: ignore[arg-type]
    }
    raw = repr(sorted(payload["properties"].items()))  # type: ignore[arg-type]
    raw += "|" + ",".join(payload["required"])
    return short_hash(raw)
