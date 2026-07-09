# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Type coercion for argument extraction.

The raw value returned by a ``FieldFinder`` may not match
the JSON-Schema type the Tool expects. This module turns
the raw value into the right scalar type, or returns
``None`` if the coercion fails (we drop the field rather
than raise).

Iter 28: moved from
``kntgraph.agents.knowledge.argument_extractor._coerce`` to
the framework. Pure logic, zero third-party deps.
"""

from __future__ import annotations

from typing import Optional, Union

from kntgraph.core._typing import ValidatorInput
from kntgraph.tools.schema import FieldSpec


# The coerced value is a JSON scalar (the FieldSpec's
# json_type is one of "string", "integer", "number").
CoercedValue = Union[str, int, float, None]


def coerce(value: ValidatorInput, spec: FieldSpec) -> Optional[CoercedValue]:
    """
    Coerce the raw value returned by a `FieldFinder`
    into the type the JSON-Schema expects.

    Returns `None` if the value cannot be coerced --
    the field is then dropped from the result. We do
    NOT raise: a `number` field that came back as
    "dozens" is best omitted, not a hard error.

    String format validation is intentionally minimal:
      - `date` / `date-time` -- kept as the raw string
        (the Tool validates downstream; we don't
        rewrite dates silently).
      - everything else -- no transformation.
    """
    if value is None:
        return None
    if spec.json_type == "string":
        return _coerce_string(value)
    if spec.json_type in ("number", "integer"):
        return _coerce_number(value, spec.json_type)
    return None


def _coerce_string(value: ValidatorInput) -> Optional[str]:
    """Coerce to a non-empty string. Empty / whitespace
    becomes ``None`` so the field is dropped.
    """
    if not isinstance(value, str):
        value = str(value)
    v = value.strip()
    return v if v else None


def _coerce_number(
    value: ValidatorInput, json_type: str
) -> Optional[Union[int, float]]:
    """Coerce to ``int`` (when ``json_type='integer'``) or
    ``float``. ``bool`` is rejected (booleans are
    technically ints in Python but we never want a
    sneak boolean field).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _coerce_numeric(value, json_type)
    if isinstance(value, str):
        return _coerce_numeric_string(value, json_type)
    return None


def _coerce_numeric(
    value: Union[int, float], json_type: str
) -> Optional[Union[int, float]]:
    """Coerce an already-numeric value. ``integer`` and a
    non-integral float returns ``None``.
    """
    if json_type == "integer" and not isinstance(value, int):
        f = float(value)
        if not f.is_integer():
            return None
        return int(f)
    return value


def _coerce_numeric_string(value: str, json_type: str) -> Optional[Union[int, float]]:
    """Coerce a string. ``","`` is normalised to ``"."`` so
    Brazilian-style decimals work.
    """
    s = value.strip().replace(",", ".")
    try:
        if json_type == "integer":
            return int(s)
        return float(s)
    except ValueError:
        return None


__all__ = ["CoercedValue", "coerce"]
