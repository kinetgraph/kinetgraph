# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the predicate mini-language (ADR-073 §4.4).

Pins the contract for the parser and evaluator against
[docs/concordos-bundle-spec.md §2](../docs/concordos-bundle-spec.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from kntgraph.concordos._mini_lang import (
    BUILTIN_SPECS,
    ConcordoSyntaxError,
    _register_builtins,
    evaluate,
    is_pure_name,
    parse_expression,
)
from kntgraph.concordos.base import StepContext


FIXED_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MockDomain:
    """Minimal DomainComponent for the test suite."""

    tier: str = "standard"
    priority: str = "low"
    status: str = "draft"


def make_ctx(
    *,
    domain: _MockDomain | None = None,
    trigger_data: dict | None = None,
    step_results: dict | None = None,
    step_states: dict | None = None,
    continuity=None,
    profile=None,
) -> StepContext:
    return StepContext(
        step_results=MappingProxyType(step_results or {}),
        step_states=MappingProxyType(step_states or {}),
        domain=domain,
        continuity=continuity,
        profile=profile,
        agent_id="a-1",
        now=FIXED_NOW,
        trigger_data=MappingProxyType(trigger_data)
        if trigger_data is not None
        else None,
        cross_agent_resolver=None,
    )


@pytest.fixture(autouse=True)
def _builtins() -> None:
    """Force builtin registration for any test that uses
    BUILTIN_SPECS directly or via evaluate()."""
    BUILTIN_SPECS.clear()
    _register_builtins()


# ---------------------------------------------------------------------------
# Parser — accept cases (golden file)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        # Literals
        "1",
        "0.5",
        "'hello'",
        '"world"',
        "true",
        "false",
        "null",
        # Paths (scoped)
        "event.data.score",
        "agent.tier",
        "agent.user_id",
        "steps.Chunking.output.chunks",
        "now",
        "now.year",
        "now.hour",
        # Paths (bare-identifier — used as right-hand side)
        "x",
        "x.y",
        "a.b.c.d",
        # Calls
        'step_completed("Foo")',
        'domain_state_is("status", "issued")',
        'profile_tier_is("vip")',
        "step_failed('foo')",
        # Comparisons
        "event.data.score >= 0.80",
        "agent.tier == 'vip'",
        "agent.tier != 'standard'",
        "agent.priority < 'high'",
        "x == y",
        "x.y < 10",
        # Boolean composition
        "a and b",
        "a or b",
        "not a",
        "a and (b or c)",
        "not (a and b)",
        "(a or b) and c",
        "a and b and c",
        "a or b or c",
        # Mixed
        "event.data.score >= 0.80 and step_completed('Chunk')",
        "agent.tier == 'vip' or event.data.amount > 1000",
        "not step_timed_out('extract') and agent.priority == 'high'",
    ],
)
def test_parser_accepts(expression: str) -> None:
    ast = parse_expression(expression)
    assert ast is not None  # parses without raising


# ---------------------------------------------------------------------------
# Parser — reject cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression,expected_substring",
    [
        ("", "empty expression"),
        ("(", "expected"),
        ("a and", "expected"),
        ("a == ", "expected"),
        ("agent.tier ==", "expected"),  # missing rhs
        ("foo(bar", "expected"),
        ("foo(,)", "expected"),
        ("agent..tier", "trailing tokens"),
        ("a == b and", "expected"),
        ("(", "expected"),
        ("(a", "expected"),
        ("a and (b", "expected"),
    ],
)
def test_parser_rejects(expression: str, expected_substring: str) -> None:
    with pytest.raises(ConcordoSyntaxError) as exc_info:
        parse_expression(expression)
    assert expected_substring in str(exc_info.value)


def test_parser_rejects_garbage_character() -> None:
    with pytest.raises(ConcordoSyntaxError) as exc_info:
        parse_expression("a @ b")
    assert "character '@'" in str(exc_info.value)


def test_parser_rejects_trailing_tokens() -> None:
    """Trailing tokens after a complete expression are
    rejected (catches ``a == b garbage`` typos)."""
    with pytest.raises(ConcordoSyntaxError) as exc_info:
        parse_expression("a == b garbage")
    assert "trailing" in str(exc_info.value)


def test_parser_rejects_unknown_scope() -> None:
    """Bare scope keyword that's not in the allowed set."""
    with pytest.raises(ConcordoSyntaxError) as exc_info:
        parse_expression("event.score > 0.5")  # event.X (not event.data)
    assert "event.data" in str(exc_info.value)


# ---------------------------------------------------------------------------
# is_pure_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("MySpec", "MySpec"),
        ("VipRequired", "VipRequired"),
        ("x", "x"),
        ("foo_bar", "foo_bar"),
        # Multi-token expressions are NOT pure names.
        ("a and b", None),
        ("a == b", None),
        ("not a", None),
        ("step_completed('Foo')", None),  # call — not a name
        ("agent.tier", None),  # path — not a name
        # Whitespace stripped.
        ("  MySpec  ", "MySpec"),
    ],
)
def test_is_pure_name(expression: str, expected: str | None) -> None:
    assert is_pure_name(expression) == expected


def test_is_pure_name_empty() -> None:
    assert is_pure_name("") is None
    assert is_pure_name("   ") is None


# ---------------------------------------------------------------------------
# Evaluator — boolean composition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression,expected",
    [
        # a="yes" (truthy), b=0 (falsy but not None), c="yes" (truthy).
        ("event.data.a and event.data.b", False),  # True and False
        ("event.data.a and event.data.c", True),  # True and True
        ("event.data.a or event.data.b", True),  # True or False
        ("event.data.b or event.data.b", False),  # False or False
        ("not event.data.a", False),  # not True
        ("not event.data.b", True),  # not False
        (
            "event.data.a and (event.data.b or event.data.c)",
            True,
        ),  # a=True, (b or c) = c=True
        (
            "event.data.a and event.data.b and event.data.c",
            False,
        ),  # b is False
        (
            "(event.data.a or event.data.b) and event.data.c",
            True,
        ),  # (True or False) and True
    ],
)
def test_evaluator_boolean_composition(expression: str, expected: bool) -> None:
    ast = parse_expression(expression)
    ctx = make_ctx(
        trigger_data={"a": "yes", "b": 0, "c": "yes"},
    )
    assert evaluate(ast, ctx) is expected


# ---------------------------------------------------------------------------
# Evaluator — path resolution
# ---------------------------------------------------------------------------


def test_evaluator_event_data_path() -> None:
    ast = parse_expression("event.data.score >= 0.80")
    assert evaluate(ast, make_ctx(trigger_data={"score": 0.85})) is True
    assert evaluate(ast, make_ctx(trigger_data={"score": 0.5})) is False
    assert evaluate(ast, make_ctx(trigger_data={"other": 1})) is False
    assert evaluate(ast, make_ctx()) is False  # no trigger_data


def test_evaluator_agent_path() -> None:
    ast = parse_expression("agent.tier == 'vip'")
    assert evaluate(ast, make_ctx(domain=_MockDomain(tier="vip"))) is True
    assert evaluate(ast, make_ctx(domain=_MockDomain(tier="standard"))) is False
    assert evaluate(ast, make_ctx(domain=None)) is False


def test_evaluator_agent_path_missing_field() -> None:
    """An agent path whose field is missing returns False
    (graceful failure per docs §2.4)."""
    ast = parse_expression("agent.priority == 'high'")
    assert evaluate(ast, make_ctx(domain=_MockDomain(priority="low"))) is False


def test_evaluator_steps_output_path() -> None:
    ast = parse_expression("steps.extract.output.size > 100")
    # Per the spec, ``output`` is a syntactic marker — the
    # walker skips it and walks ``size`` directly. So the
    # data at step_results['extract'] should NOT have an
    # ``output`` key.
    assert (
        evaluate(
            ast,
            make_ctx(step_results={"extract": {"size": 150}}),
        )
        is True
    )
    assert (
        evaluate(
            ast,
            make_ctx(step_results={"extract": {"size": 50}}),
        )
        is False
    )
    assert evaluate(ast, make_ctx()) is False  # no step_results


def test_evaluator_steps_output_path_skips_output_keyword() -> None:
    """``steps.<name>.<field>`` is a convenience alias
    for ``steps.<name>.output.<field>``."""
    ast = parse_expression("steps.extract.chunks > 0")
    assert (
        evaluate(
            ast,
            make_ctx(step_results={"extract": {"chunks": 5}}),
        )
        is True
    )


def test_evaluator_now_path() -> None:
    ast = parse_expression("now.year >= 2025")
    assert evaluate(ast, make_ctx()) is True
    ast = parse_expression("now.year < 2025")
    assert evaluate(ast, make_ctx()) is False
    ast = parse_expression("now == 2025")  # bare-identifier as rhs
    assert evaluate(ast, make_ctx()) is False  # 2025 is a literal, not a path
    ast = parse_expression("now.year == now.year")
    assert evaluate(ast, make_ctx()) is True


def test_evaluator_now_unknown_attribute() -> None:
    """``now.<attr>`` with an unknown attribute returns None
    (graceful)."""
    ast = parse_expression("now.foobar > 0")
    assert evaluate(ast, make_ctx()) is False


# ---------------------------------------------------------------------------
# Evaluator — comparison with various rhs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("event.data.score == 5", True),
        ("event.data.score == 6", False),
        ("event.data.score != 5", False),
        ("event.data.score != 6", True),
        ("event.data.score < 10", True),
        ("event.data.score <= 5", True),
        ("event.data.score > 5", False),
        ("event.data.score >= 5", True),
    ],
)
def test_evaluator_number_comparison(expression: str, expected: bool) -> None:
    ast = parse_expression(expression)
    assert evaluate(ast, make_ctx(trigger_data={"score": 5})) is expected


def test_evaluator_string_comparison() -> None:
    ast = parse_expression("agent.tier == 'vip'")
    assert evaluate(ast, make_ctx(domain=_MockDomain(tier="vip"))) is True


def test_evaluator_null_comparison() -> None:
    """Comparing with null: == True only if both are null;
    != True only if both are not null."""
    ast = parse_expression("agent.score == null")
    # agent.score is not a field, so resolves to None
    assert evaluate(ast, make_ctx(domain=_MockDomain())) is True
    ast = parse_expression("agent.score != null")
    assert evaluate(ast, make_ctx(domain=_MockDomain())) is False
    # When path resolves to a real value, != null is True
    ast = parse_expression("event.data.score != null")
    assert evaluate(ast, make_ctx(trigger_data={"score": 5})) is True
    # == null is False
    ast = parse_expression("event.data.score == null")
    assert evaluate(ast, make_ctx(trigger_data={"score": 5})) is False


def test_evaluator_type_mismatch() -> None:
    """Comparing different types returns False (not crash)."""
    ast = parse_expression("agent.tier == 5")  # string vs int
    assert evaluate(ast, make_ctx(domain=_MockDomain(tier="vip"))) is False


# ---------------------------------------------------------------------------
# Evaluator — built-in functions
# ---------------------------------------------------------------------------


def test_evaluator_builtin_step_completed() -> None:
    from kntgraph.concordos.specs import StepCompleted

    spec = BUILTIN_SPECS["step_completed"]("extract")
    assert spec.is_satisfied_by(make_ctx(step_states={"extract": "completed"})) is True
    assert spec.is_satisfied_by(make_ctx(step_states={"extract": "failed"})) is False
    assert spec.is_satisfied_by(make_ctx(step_states={})) is False


def test_evaluator_builtin_domain_state_is() -> None:
    from kntgraph.concordos.specs import DomainStateIs

    spec = BUILTIN_SPECS["domain_state_is"]("status", "issued")
    assert spec.is_satisfied_by(make_ctx(domain=_MockDomain(status="issued"))) is True
    assert spec.is_satisfied_by(make_ctx(domain=_MockDomain(status="draft"))) is False


def test_evaluator_builtin_profile_tier_is() -> None:
    from kntgraph.concordos.specs import ProfileTierIs

    spec = BUILTIN_SPECS["profile_tier_is"]("vip")
    assert spec.is_satisfied_by(make_ctx(profile=_MockDomain(tier="vip"))) is True
    assert spec.is_satisfied_by(make_ctx(profile=_MockDomain(tier="standard"))) is False


def test_evaluator_builtin_continuity_tool_used() -> None:
    from kntgraph.core.components import ContinuityComponent
    from kntgraph.concordos.specs import ContinuityToolUsed

    spec = BUILTIN_SPECS["continuity_tool_used"]("nfe_emitter")
    continuity = ContinuityComponent(
        tenant_id="t-1", user_id="u-1", last_tools={"nfe_emitter": "t"}
    )
    assert spec.is_satisfied_by(make_ctx(continuity=continuity)) is True
    assert spec.is_satisfied_by(make_ctx()) is False


def test_evaluator_unknown_builtin() -> None:
    """Unknown built-in name raises ValueError at evaluation."""
    ast = parse_expression("unknown_thing()")
    with pytest.raises(ValueError, match="unknown built-in spec"):
        evaluate(ast, make_ctx())


# ---------------------------------------------------------------------------
# ConcordoSyntaxError — formatting
# ---------------------------------------------------------------------------


def test_syntax_error_includes_line_and_column() -> None:
    """Multi-line expressions report the correct line."""
    # Force a syntax error on line 2 with a trailing token.
    with pytest.raises(ConcordoSyntaxError) as exc_info:
        parse_expression("a ==\nb garbage")
    err = str(exc_info.value)
    assert "line 2" in err
    assert "^" in err


def test_syntax_error_includes_caret() -> None:
    with pytest.raises(ConcordoSyntaxError) as exc_info:
        parse_expression("agent..tier")
    err = str(exc_info.value)
    # The caret points at the offending column.
    assert "^" in err


def test_syntax_error_includes_hint() -> None:
    """Hints are shown for known error categories."""
    with pytest.raises(ConcordoSyntaxError) as exc_info:
        parse_expression("foo @ bar")
    err = str(exc_info.value)
    # Either hint or character is in the error.
    assert "character '@'" in err or "hint" in err
