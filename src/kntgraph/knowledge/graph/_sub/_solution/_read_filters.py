# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._sub._solution._read_filters -- read-path
query composition for ``GraphSolutionAdapter``.

The read path is the most complex part of the Solution
sub-adapter: the optional filters (tags, tool_name, status)
must be composed at call time because the Cypher syntax
depends on the combination. This module extracts the
template strings and the WHERE-clause builders so the
class body in :mod:`kntgraph.knowledge.graph._sub._solution._adapter`
stays focused on the public write + read API.

Why module-level (not class methods)
------------------------------------

The builders are stateless: they take a filter argument
and return a fragment + parameters. They don't read any
``self`` state. Hoisting them to module-level keeps each
helper at CC <= 2 (down from CC = 2 inside the class with
``@staticmethod`` boilerplate) and lets ``_adapter.py``
focus on the public API.

This module is a private implementation detail of
``_solution``; the public surface is unchanged.
"""

from __future__ import annotations

import json
from string import Template
from typing import Optional

from ._row_helpers import _float_at, _obj_at, _str_at


# ---------------------------------------------------------------------------
# Base read-path Cypher templates.
#
# We use ``string.Template`` (``$edge_match``,
# ``$where_clause``) instead of ``str.format`` so the
# Cypher-side ``$vec`` / ``$tool_name`` / ``$k`` parameters
# survive the substitution intact. ``str.format`` would
# interpret any ``$identifier`` as a format key, breaking
# the query.
# ---------------------------------------------------------------------------

BASE_FIND_SOLUTIONS_BY_PROBLEM = Template("""
MATCH (p:Problem)$edge_match->(a:Action)-[:ON_TOOL]->(t:Tool)
MATCH (a)-[:PRODUCED]->(o:Outcome)
WHERE p.embedding IS NOT NULL$where_clause
WITH p, a, t, o, vec.cosineDistance(p.embedding, vecf32($vec)) AS score
ORDER BY score ASC LIMIT $k
RETURN p.fingerprint AS problem_fingerprint,
       p.tags_json AS problem_tags_json,
       a.request_event_id AS action_request_event_id,
       a.tool_name AS action_tool_name,
       a.params_json AS action_params_json,
       t.name AS tool_name,
       o.status AS outcome_status,
       o.confidence AS outcome_confidence,
       o.latency_ms AS outcome_latency_ms,
       o.error_message AS outcome_error_message,
       p.last_validated_at AS last_validated_at,
       score
""")


BASE_FIND_SOLUTIONS_BY_TOOL = Template("""
MATCH (a:Action)-[:ON_TOOL]->(t:Tool {name: $tool_name})
MATCH (p:Problem)$edge_match->(a)
MATCH (a)-[:PRODUCED]->(o:Outcome)
WHERE 1=1$where_clause
RETURN p.fingerprint AS problem_fingerprint,
       p.tags_json AS problem_tags_json,
       a.request_event_id AS action_request_event_id,
       a.tool_name AS action_tool_name,
       a.params_json AS action_params_json,
       t.name AS tool_name,
       o.status AS outcome_status,
       o.confidence AS outcome_confidence
ORDER BY o.confidence DESC LIMIT $k
""")


# ---------------------------------------------------------------------------
# WHERE-clause builders.
# ---------------------------------------------------------------------------


def edge_match_for_status(status: str) -> str:
    """
    Translate a status into the Problem->Action
    edge-type constraint.

    - ``"completed"`` -> ``-[r:SOLVED_BY]``
    - ``"failed"`` -> ``-[r:FAILED_WITH]``
    - ``"all"`` -> ``-[r:SOLVED_BY|FAILED_WITH]``
    """
    if status == "failed":
        return "-[r:FAILED_WITH]"
    if status == "all":
        return "-[r:SOLVED_BY|FAILED_WITH]"
    return "-[r:SOLVED_BY]"


def build_tags_clause(
    tags: Optional[dict[str, str]],
) -> tuple[str, list[str]]:
    """
    Build a ``tags_json CONTAINS`` filter.

    FalkorDB does not support parameter substitution
    inside CONTAINS patterns (the pattern is a
    literal). The values are JSON-encoded and
    inlined. The function returns the WHERE fragment
    and the list of inlined needles (for assertions).

    Multi-tag is AND'd: every needle must match.
    """
    if not tags:
        return "", []
    needles: list[str] = []
    for key, value in tags.items():
        needle = json.dumps({key: value}, sort_keys=True, default=str)
        inner = needle[1:-1].replace('"', '\\"')
        needles.append(inner)
    fragment = " AND " + " AND ".join(f"p.tags_json CONTAINS '{n}'" for n in needles)
    return fragment, needles


def build_tool_name_clause(
    tool_name: Optional[str],
) -> tuple[str, dict[str, str]]:
    """
    Build the ``t.name = $tool_name`` filter (parametrised).

    For find-by-tool, this filter is always present (the
    base query already binds ``$tool_name``). For
    find-by-problem, this filter is optional and added
    when the caller passes ``tool_name=``.
    """
    if not tool_name:
        return "", {}
    return " AND t.name = $tool_name", {"tool_name": tool_name}


def build_status_clause(status: str) -> tuple[str, dict[str, str]]:
    """
    Build the ``o.status = $status`` filter (parametrised).

    Empty when ``status='all'`` (no restriction).
    """
    if status == "all":
        return "", {}
    return " AND o.status = $status", {"status": status}


# ---------------------------------------------------------------------------
# Row mappers.
# ---------------------------------------------------------------------------


def row_to_solution_by_problem(row: list) -> dict:
    """
    Map a FalkorDB result row from the
    ``find_solutions_by_problem`` query into a dict.

    Pulls out 12 columns (problem / action / tool /
    outcome / score). Each field has a guard against
    short rows (some optional columns may be missing
    on legacy data).
    """
    return {
        "problem_fingerprint": _str_at(row, 0, ""),
        "problem_tags_json": _str_at(row, 1, "{}"),
        "action_request_event_id": _str_at(row, 2, ""),
        "action_tool_name": _str_at(row, 3, ""),
        "action_params_json": _str_at(row, 4, "{}"),
        "tool_name": _str_at(row, 5, ""),
        "outcome_status": _str_at(row, 6, ""),
        "outcome_confidence": _float_at(row, 7, 0.0),
        "outcome_latency_ms": _obj_at(row, 8, None),
        "outcome_error_message": _str_at(row, 9, ""),
        "last_validated_at": _obj_at(row, 10, None),
        "score": _float_at(row, 11, 0.0),
    }


def row_to_solution_by_tool(row: list) -> dict:
    """
    Map a FalkorDB result row from the
    ``find_solutions_by_tool`` query into a dict.

    Smaller projection (8 columns, no score / latency).
    """
    return {
        "problem_fingerprint": _str_at(row, 0, ""),
        "problem_tags_json": _str_at(row, 1, "{}"),
        "action_request_event_id": _str_at(row, 2, ""),
        "action_tool_name": _str_at(row, 3, ""),
        "action_params_json": _str_at(row, 4, "{}"),
        "tool_name": _str_at(row, 5, ""),
        "outcome_status": _str_at(row, 6, ""),
        "outcome_confidence": _float_at(row, 7, 0.0),
    }


__all__ = [
    "BASE_FIND_SOLUTIONS_BY_PROBLEM",
    "BASE_FIND_SOLUTIONS_BY_TOOL",
    "build_status_clause",
    "build_tags_clause",
    "build_tool_name_clause",
    "edge_match_for_status",
    "row_to_solution_by_problem",
    "row_to_solution_by_tool",
]
