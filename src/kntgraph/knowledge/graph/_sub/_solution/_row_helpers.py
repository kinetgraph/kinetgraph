# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._sub._solution._row_helpers -- row-mapper primitives.

Pure helpers used by the row-mappers in
:mod:`kntgraph.knowledge.graph._sub._solution._read_filters`
to extract typed values from a FalkorDB result row without
branching at every call site. Kept module-level (not methods)
so the call sites read as plain function calls and stay
CC = 1 per mapping function.

This module is a private implementation detail of
``_solution``; the public surface is unchanged.
"""

from __future__ import annotations


def _str_at(row: list, idx: int, default: str) -> str:
    """Return ``row[idx]`` as a string, or ``default`` if
    the row is too short or the value is empty/None.
    """
    if len(row) <= idx or not row[idx]:
        return default
    return str(row[idx])


def _float_at(row: list, idx: int, default: float) -> float:
    """Return ``row[idx]`` as a float, or ``default``."""
    if len(row) <= idx or row[idx] is None:
        return default
    return float(row[idx])


def _obj_at(row: list, idx: int, default: object) -> object:
    """Return ``row[idx]`` as-is, or ``default`` if the
    row is too short.
    """
    if len(row) <= idx:
        return default
    return row[idx]


__all__ = ["_str_at", "_float_at", "_obj_at"]
