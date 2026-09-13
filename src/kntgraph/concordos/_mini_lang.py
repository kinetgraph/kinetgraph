# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
concordos._mini_lang -- the predicate mini-language (ADR-073 §4.4).

A small, declarative expression language for FSM guards and
Saga conditions. The grammar is defined in
[docs/concordos-bundle-spec.md §2](../docs/concordos-bundle-spec.md).

Two forms
---------

A predicate string is classified syntactically:

  - **Name lookup**: no parens, no operator, no path, no number.
    Resolved against ``SpecRegistry`` (built-ins + app-registered).
  - **Expression**: anything else. Parsed by the recursive-descent
    parser and evaluated against a ``StepContext``.

Grammar
-------

::

    expr        = or_expr
    or_expr     = and_expr { "or" and_expr }
    and_expr    = not_expr { "and" not_expr }
    not_expr    = "not" not_expr | atom
    atom        = comparison | call | path | "(" expr ")" | literal
    comparison  = path comp_op value
    comp_op     = "==" | "!=" | "<=" | ">=" | "<" | ">"
    value       = number | string | "true" | "false" | "null"
    call        = identifier "(" [ expr_list ] ")"
    expr_list   = expr { "," expr }
    path        = scope { "." identifier }
    scope       = "event.data" | "steps" | "agent" | "now"

Path scopes (resolution at evaluation)
--------------------------------------

- ``event.data.<field>``: the trigger event's data payload
  (``ctx.trigger_data[field]``).
- ``steps.<name>.output.<field>``: step result lookup
  (``ctx.step_results[name][field]``).
- ``agent.<field>``: DomainComponent attribute
  (``getattr(ctx.domain, field, None)``).
- ``now`` or ``now.<attr>``: the dispatcher's current tick
  timestamp (``ctx.now``).

Built-in functions
------------------

``step_completed(name)``, ``step_failed(name)``, ``step_timed_out(name)``,
``domain_state_is(field, value)``, ``profile_tier_is(tier)``,
``continuity_tool_used(name)``. See
[docs/concordos-bundle-spec.md §4](../docs/concordos-bundle-spec.md).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Mapping

if TYPE_CHECKING:
    from datetime import datetime

    from .base import StepContext


__all__ = [
    "ConcordoSyntaxError",
    "Expr",
    "parse_expression",
    "evaluate",
    "is_pure_name",
    "BUILTIN_SPECS",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConcordoSyntaxError(ValueError):
    """Raised by the parser on a malformed expression.

    Carries line/column so the CLI's ``validate`` command
    can print a caret-pointed diagnostic.
    """

    def __init__(
        self,
        expression: str,
        line: int,
        column: int,
        expected: str,
        found: str,
        hint: str | None = None,
    ) -> None:
        self.expression = expression
        self.line = line
        self.column = column
        self.expected = expected
        self.found = found
        self.hint = hint
        # Build a multi-line diagnostic with a caret.
        lines = expression.splitlines() or [""]
        caret_line = "^".rjust(column + 1).rjust(column + 2)
        snippet = (
            f"line {line}, col {column}\n"
            f"  {lines[line - 1] if line - 1 < len(lines) else ''}\n"
            f"  {caret_line}\n"
            f"expected: {expected}\n"
            f"found:    {found}\n"
        )
        if hint:
            snippet += f"hint:     {hint}\n"
        super().__init__(snippet.rstrip())

    def __str__(self) -> str:
        return self.args[0] if self.args else super().__str__()


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expr:
    """Base class for AST nodes."""


@dataclass(frozen=True)
class NameLookup(Expr):
    """An identifier not followed by parens, operators, or
    paths. Resolved against the SpecRegistry at evaluation
    time (or compile time, depending on the caller)."""

    name: str


@dataclass(frozen=True)
class Or(Expr):
    left: Expr
    right: Expr


@dataclass(frozen=True)
class And(Expr):
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Not(Expr):
    operand: Expr


@dataclass(frozen=True)
class Compare(Expr):
    """A single comparison: ``path op value``."""

    path: "Path"
    op: str  # "==" | "!=" | "<=" | ">=" | "<" | ">"
    value: "Literal"


@dataclass(frozen=True)
class Call(Expr):
    """A built-in function call: ``name(arg1, arg2, ...)``."""

    name: str
    args: tuple[Expr, ...]


@dataclass(frozen=True)
class Path(Expr):
    """A dotted path: ``scope.tail[0].tail[1]...``."""

    scope: str  # "event.data" | "steps" | "agent" | "now"
    tail: tuple[str, ...]


@dataclass(frozen=True)
class Literal(Expr):
    """A literal value (string, number, bool, null)."""

    value: Any  # str | int | float | bool | None


# Operators recognised by the parser.
_COMPARISONS: ClassVar[frozenset[str]] = frozenset({"==", "!=", "<=", ">=", "<", ">"})


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

# Token kinds. (kind, lexeme) tuples are emitted by the
# tokenizer; the parser consumes them in order.
_TOKEN_KIND = enum.Enum(
    "_TOKEN_KIND",
    "IDENT NUMBER STRING OP LPAREN RPAREN COMMA DOT",
)


# The tokenizer matches whitespace, strings, numbers,
# operators, parens, commas, dots, and identifiers. Identifiers
# do NOT include dots (paths are split into IDENT DOT IDENT at
# the lexer level). Comments start with ``#`` and run to end
# of line.
_TOKEN_RE = re.compile(
    r"""
    \s+                                                  # whitespace (skipped)
  | \# [^\n]*                                             # comment (skipped)
  | (?P<STRING_S> '(?: \\' | [^'] )* ' )                 # single-quoted string
  | (?P<STRING_D> "(?: \\" | [^"] )* " )                 # double-quoted string
  | (?P<NUMBER> \d+ (?: \.\d+ )? )                       # integer or float
  | (?P<OP_GE> >= )                                      # >=
  | (?P<OP_LE> <= )                                      # <=
  | (?P<OP_EQ> == )                                      # ==
  | (?P<OP_NE> != )                                      # !=
  | (?P<OP_GT> > )                                       # >
  | (?P<OP_LT> < )                                       # <
  | (?P<LPAREN> \( )                                     # (
  | (?P<RPAREN> \) )                                     # )
  | (?P<COMMA> , )                                       # ,
  | (?P<DOT> \. )                                        # .
  | (?P<IDENT> [A-Za-z_][A-Za-z0-9_]* )                 # identifier (no dots)
  """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Token:
    kind: str  # _TOKEN_KIND.name
    lexeme: str
    line: int
    column: int  # 1-indexed


def _tokenize(expression: str) -> list[_Token]:
    """Tokenise ``expression`` into ``_Token`` objects.

    Tracks line and column so the parser can attach them
    to ``ConcordoSyntaxError``. Strings and numbers carry
    their literal value in ``lexeme``; identifiers and
    operators carry their text.
    """
    tokens: list[_Token] = []
    line = 1
    col = 1
    pos = 0
    while pos < len(expression):
        m = _TOKEN_RE.match(expression, pos)
        if m is None:
            # Find the line/col of the offending position.
            offending = expression[pos]
            kind = (
                "string terminator"
                if offending in ("'", '"')
                else f"character {offending!r}"
            )
            raise ConcordoSyntaxError(
                expression=expression,
                line=line,
                column=col,
                expected="token",
                found=f"unexpected {kind}",
                hint="check for unmatched quotes or operators",
            )
        # Whitespace / comments: update line/col, no token.
        if m.lastgroup is None:
            text = m.group(0)
            nl = text.count("\n")
            if nl:
                line += nl
                col = len(text) - text.rfind("\n")
            else:
                col += len(text)
            pos = m.end()
            continue
        text = m.group(0)
        # Compute the line/col at the START of this token.
        # We already advanced line/col to the start above
        # via the whitespace-only branch.
        if m.lastgroup in ("STRING_S", "STRING_D"):
            kind = _TOKEN_KIND.STRING.name
        elif m.lastgroup == "NUMBER":
            kind = _TOKEN_KIND.NUMBER.name
        elif m.lastgroup in ("OP_GE", "OP_LE", "OP_EQ", "OP_NE", "OP_GT", "OP_LT"):
            kind = _TOKEN_KIND.OP.name
        elif m.lastgroup == "LPAREN":
            kind = _TOKEN_KIND.LPAREN.name
        elif m.lastgroup == "RPAREN":
            kind = _TOKEN_KIND.RPAREN.name
        elif m.lastgroup == "COMMA":
            kind = _TOKEN_KIND.COMMA.name
        elif m.lastgroup == "DOT":
            kind = _TOKEN_KIND.DOT.name
        elif m.lastgroup == "IDENT":
            kind = _TOKEN_KIND.IDENT.name
        else:
            raise ConcordoSyntaxError(  # pragma: no cover
                expression=expression,
                line=line,
                column=col,
                expected="token",
                found=f"unknown group {m.lastgroup}",
            )
        tokens.append(_Token(kind=kind, lexeme=text, line=line, column=col))
        # Update line/col past this token. Strings may
        # contain newlines (after escape sequences — we
        # don't unescape here, but the line count matters).
        nl = text.count("\n")
        if nl:
            line += nl
            col = len(text) - text.rfind("\n")
        else:
            col += len(text)
        pos = m.end()
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class _Parser:
    """Recursive-descent parser. Consumes ``_Token`` objects
    produced by ``_tokenize`` and returns an ``Expr`` AST.

    The grammar is in the module docstring.
    """

    def __init__(self, expression: str, tokens: list[_Token]) -> None:
        self._expression = expression
        self._tokens = tokens
        self._i = 0

    def _peek(self) -> _Token | None:
        return self._tokens[self._i] if self._i < len(self._tokens) else None

    def _peek_at(self, offset: int) -> _Token | None:
        i = self._i + offset
        return self._tokens[i] if i < len(self._tokens) else None

    def _consume(self) -> _Token:
        tok = self._tokens[self._i]
        self._i += 1
        return tok

    def _expect(self, kind: str, found_hint: str) -> _Token:
        tok = self._peek()
        if tok is None or tok.kind != kind:
            raise ConcordoSyntaxError(
                expression=self._expression,
                line=tok.line if tok else len(self._expression.splitlines()),
                column=tok.column if tok else len(self._expression),
                expected=found_hint,
                found=(
                    f"end of input" if tok is None else f"{tok.kind}({tok.lexeme!r})"
                ),
            )
        return self._consume()

    # expr = or_expr
    def parse_expr(self) -> Expr:
        return self._parse_or()

    # or_expr = and_expr { "or" and_expr }
    def _parse_or(self) -> Expr:
        left = self._parse_and()
        while True:
            tok = self._peek()
            if tok is None or tok.kind != "IDENT" or tok.lexeme != "or":
                return left
            self._consume()
            right = self._parse_and()
            left = Or(left, right)

    # and_expr = not_expr { "and" not_expr }
    def _parse_and(self) -> Expr:
        left = self._parse_not()
        while True:
            tok = self._peek()
            if tok is None or tok.kind != "IDENT" or tok.lexeme != "and":
                return left
            self._consume()
            right = self._parse_not()
            left = And(left, right)

    # not_expr = "not" not_expr | atom
    def _parse_not(self) -> Expr:
        tok = self._peek()
        if tok is not None and tok.kind == "IDENT" and tok.lexeme == "not":
            self._consume()
            operand = self._parse_not()
            return Not(operand=operand)
        return self._parse_atom_with_comparison()

    # atom-with-comparison: comparison | call | path | "(" expr ")" | literal
    def _parse_atom_with_comparison(self) -> Expr:
        """Top-level atom. A path-followed-by-op becomes
        a comparison (``path op value``); otherwise it's
        just a path or other atom form."""
        tok = self._peek()
        if tok is None:
            raise ConcordoSyntaxError(
                expression=self._expression,
                line=len(self._expression.splitlines()),
                column=len(self._expression),
                expected="expression",
                found="end of input",
            )
        # Parenthesised expression
        if tok.kind == "LPAREN":
            self._consume()
            inner = self.parse_expr()
            self._expect("RPAREN", "')'")
            return inner
        # Literal: string or number
        if tok.kind == "STRING":
            self._consume()
            value = _parse_string_literal(tok.lexeme)
            return Literal(value=value)
        if tok.kind == "NUMBER":
            self._consume()
            value = _parse_number_literal(tok.lexeme)
            return Literal(value=value)
        # Identifier-based atoms: name lookup, call, or path.
        if tok.kind == "IDENT":
            return self._parse_ident_atom_with_comparison()
        raise ConcordoSyntaxError(
            expression=self._expression,
            line=tok.line,
            column=tok.column,
            expected="expression",
            found=f"{tok.kind}({tok.lexeme!r})",
        )

    def _parse_ident_atom_with_comparison(self) -> Expr:
        """Handle IDENT-start atom; if followed by an OP,
        parse as a comparison (``path op value``).
        """
        first = self._consume()  # IDENT
        lex = first.lexeme
        # 1. Call: IDENT "(" [ expr_list ] ")"
        nxt = self._peek()
        if nxt is not None and nxt.kind == "LPAREN":
            self._consume()
            args: list[Expr] = []
            if self._peek() is not None and self._peek().kind != "RPAREN":
                args.append(self.parse_expr())
                while self._peek() is not None and self._peek().kind == "COMMA":
                    self._consume()
                    args.append(self.parse_expr())
            self._expect("RPAREN", "')'")
            return Call(name=lex, args=tuple(args))
        # 2. Path (with optional comparison)
        if lex in ("steps", "agent", "now"):
            return self._parse_path_or_comparison(lex)
        if lex == "event" and self._peek_at(0) is not None:
            if (
                self._peek_at(0).kind == "DOT"
                and self._peek_at(1) is not None
                and self._peek_at(1).kind == "IDENT"
                and self._peek_at(1).lexeme == "data"
                and self._peek_at(2) is not None
                and self._peek_at(2).kind == "DOT"
            ):
                return self._parse_path_or_comparison(lex)
        # 3. Bare identifier: if followed by OP (start of
        # comparison) or DOT (path continuation), parse as
        # path. The IDENT was already consumed at the top
        # of this method; ``_parse_path_or_comparison``
        # picks up from the current position.
        nxt = self._peek()
        if nxt is not None and (nxt.kind == "OP" or nxt.kind == "DOT"):
            return self._parse_path_or_comparison(lex)
        # Bare identifier with no operator and no DOT —
        # treat as name lookup (the loader resolves it
        # via SpecRegistry).
        return NameLookup(name=lex)

    def _parse_path_or_comparison(self, first_scope: str) -> Expr:
        """Parse a path (scoped or bare), then if the next
        token is an OP, parse the comparison. Otherwise
        return the bare path.

        For scoped paths (steps/agent/now/event.data), we
        delegate to ``_parse_path``. For bare identifiers
        (``x.y``, ``foo``), we build the path manually so
        ``_parse_path`` doesn't reject ``foo`` as a
        non-existent scope.
        """
        if first_scope in ("steps", "agent", "now") or (
            first_scope == "event"
            and self._peek_at(0) is not None
            and self._peek_at(0).kind == "DOT"
        ):
            path = self._parse_path(first_scope)
        else:
            # Bare identifier: build the path manually.
            tail: list[str] = [first_scope]
            while (
                self._peek() is not None
                and self._peek().kind == "DOT"
                and self._peek_at(1) is not None
                and self._peek_at(1).kind == "IDENT"
            ):
                self._consume()  # DOT
                ident_tok = self._consume()  # IDENT
                tail.append(ident_tok.lexeme)
            path = Path(scope="", tail=tuple(tail))
        nxt = self._peek()
        if nxt is not None and nxt.kind == "OP":
            return self._parse_comparison(path)
        return path

    # alias kept for internal use; _parse_atom delegates
    # through _parse_atom_with_comparison now.
    def _parse_atom(self) -> Expr:  # pragma: no cover
        return self._parse_atom_with_comparison()

    def _parse_ident_atom(self) -> Expr:
        """Handle the three IDENT-start shapes: call,
        path, or name lookup."""
        first = self._consume()  # IDENT
        lex = first.lexeme
        # 1. Call: IDENT "(" [ expr_list ] ")"
        nxt = self._peek()
        if nxt is not None and nxt.kind == "LPAREN":
            self._consume()
            args: list[Expr] = []
            if self._peek() is not None and self._peek().kind != "RPAREN":
                args.append(self.parse_expr())
                while self._peek() is not None and self._peek().kind == "COMMA":
                    self._consume()
                    args.append(self.parse_expr())
            self._expect("RPAREN", "')'")
            return Call(name=lex, args=tuple(args))
        # 2. Path: scope { "." identifier }
        # The path scope can be a single keyword (steps,
        # agent, now) or the two-word ``event.data`` (which
        # the tokenizer splits as IDENT DOT IDENT). The
        # check happens BEFORE consuming the IDENT so we
        # look at the original token positions.
        if lex in ("steps", "agent", "now"):
            return self._parse_path(lex)
        if lex == "event" and self._peek_at(0) is not None:
            # We just consumed event (i advanced). peek(0)
            # is the next token (DOT); peek(1) is the one
            # after; peek(2) is the one after that.
            if (
                self._peek_at(0).kind == "DOT"
                and self._peek_at(1) is not None
                and self._peek_at(1).kind == "IDENT"
                and self._peek_at(1).lexeme == "data"
                and self._peek_at(2) is not None
                and self._peek_at(2).kind == "DOT"
            ):
                return self._parse_path(lex)
        # 3. Name lookup: bare identifier
        return NameLookup(name=lex)

    def _parse_path(self, first_scope: str) -> Path:
        """Parse ``scope { "." identifier }`` starting at
        the IDENT ``first_scope`` (already consumed).

        Valid scopes: ``event.data``, ``steps``, ``agent``,
        ``now``. Bare identifiers (e.g., ``x.y``) are
        accepted as paths with an empty scope (used in
        comparison right-hand sides like ``x == y``)."""
        scope = first_scope
        # Special case: ``event`` followed by ``.data`` is the
        # ``event.data`` scope (single token in path space).
        if scope == "event":
            nxt1 = self._peek()
            if nxt1 is not None and nxt1.kind == "DOT":
                self._consume()
                nxt2 = self._peek()
                if nxt2 is None or nxt2.kind != "IDENT" or nxt2.lexeme != "data":
                    raise ConcordoSyntaxError(
                        expression=self._expression,
                        line=nxt2.line if nxt2 else nxt1.line,
                        column=nxt2.column if nxt2 else nxt1.column,
                        expected="'data'",
                        found=(
                            f"{nxt2.kind}({nxt2.lexeme!r})" if nxt2 else "end of input"
                        ),
                        hint=(
                            "the only valid scope starting with "
                            "'event.' is 'event.data.'"
                        ),
                    )
                self._consume()
                scope = "event.data"
        # Bare identifiers (empty scope after this point
        # means the scope is a bare IDENT — used in
        # comparison right-hand sides).
        valid_scopes = ("event.data", "steps", "agent", "now")
        if scope != "" and scope not in valid_scopes:
            raise ConcordoSyntaxError(
                expression=self._expression,
                line=1,
                column=1,
                expected="path scope",
                found=f"unknown scope {scope!r}",
                hint=("valid scopes are 'event.data', 'steps', 'agent', and 'now'"),
            )
        tail: list[str] = []
        while (
            self._peek() is not None
            and self._peek().kind == "DOT"
            and self._peek_at(1) is not None
            and self._peek_at(1).kind == "IDENT"
        ):
            self._consume()  # DOT
            ident_tok = self._consume()  # IDENT
            tail.append(ident_tok.lexeme)
        return Path(scope=scope, tail=tuple(tail))

    # comparison = path comp_op value
    def _parse_comparison(self, path: Path) -> Compare:
        op_tok = self._expect("OP", "comparison operator")
        op = op_tok.lexeme
        if op not in _COMPARISONS:
            raise ConcordoSyntaxError(
                expression=self._expression,
                line=op_tok.line,
                column=op_tok.column,
                expected="comparison operator (==, !=, <, <=, >, >=)",
                found=f"operator({op!r})",
            )
        value = self._parse_value_or_path()
        return Compare(path=path, op=op, value=value)

    # value-or-path: literal | call | path | bare-identifier
    # (allows ``x == y`` where both are identifiers,
    # ``x == 5`` where one is literal, and
    # ``agent.tier == step_completed("Foo")`` where the rhs
    # is a built-in call.)
    def _parse_value_or_path(self) -> Expr:
        tok = self._peek()
        if tok is None:
            raise ConcordoSyntaxError(
                expression=self._expression,
                line=len(self._expression.splitlines()),
                column=len(self._expression),
                expected="comparison value",
                found="end of input",
            )
        if tok.kind in ("STRING", "NUMBER"):
            self._consume()
            return (
                Literal(value=_parse_string_literal(tok.lexeme))
                if tok.kind == "STRING"
                else Literal(value=_parse_number_literal(tok.lexeme))
            )
        if tok.kind == "IDENT" and tok.lexeme in ("true", "false", "null"):
            self._consume()
            return Literal(
                value=(
                    True
                    if tok.lexeme == "true"
                    else (False if tok.lexeme == "false" else None)
                )
            )
        if tok.kind == "IDENT":
            # 1. Call: IDENT "(" args ")"
            nxt = self._peek_at(1)
            if nxt is not None and nxt.kind == "LPAREN":
                self._consume()  # IDENT
                return self._parse_call_from_consumed_ident(tok)
            # 2. Path (scope keyword or bare identifier with .tail)
            if tok.lexeme in ("steps", "agent", "now"):
                self._consume()
                return self._parse_path(tok.lexeme)
            if tok.lexeme == "event" and self._peek_at(1) is not None:
                if (
                    self._peek_at(1).kind == "DOT"
                    and self._peek_at(2) is not None
                    and self._peek_at(2).kind == "IDENT"
                    and self._peek_at(2).lexeme == "data"
                    and self._peek_at(3) is not None
                    and self._peek_at(3).kind == "DOT"
                ):
                    self._consume()
                    return self._parse_path(tok.lexeme)
            # 3. Bare-identifier path (Path with empty scope)
            if self._peek_at(1) is not None and self._peek_at(1).kind == "DOT":
                self._consume()  # IDENT
                # Build the path manually — don't go through
                # _parse_path which validates the scope.
                return self._parse_bare_path(tok.lexeme)
            # 4. Bare identifier → NameLookup
            self._consume()
            return NameLookup(name=tok.lexeme)
        raise ConcordoSyntaxError(
            expression=self._expression,
            line=tok.line,
            column=tok.column,
            expected="literal value, call, or path",
            found=f"{tok.kind}({tok.lexeme!r})",
        )

    def _parse_bare_path(self, first_lexeme: str) -> Path:
        """Parse a bare-identifier path (no scope
        validation; the bare identifier IS the first
        tail element)."""
        tail: list[str] = [first_lexeme]
        while (
            self._peek() is not None
            and self._peek().kind == "DOT"
            and self._peek_at(1) is not None
            and self._peek_at(1).kind == "IDENT"
        ):
            self._consume()  # DOT
            ident_tok = self._consume()  # IDENT
            tail.append(ident_tok.lexeme)
        return Path(scope="", tail=tuple(tail))

    def _parse_call_from_consumed_ident(self, ident: _Token) -> Call:
        """Parse ``IDENT "(" args ")"`` where the IDENT has
        already been consumed (used inside
        _parse_value_or_path)."""
        self._expect("LPAREN", "'(' after function name")
        args: list[Expr] = []
        if self._peek() is not None and self._peek().kind != "RPAREN":
            args.append(self.parse_expr())
            while self._peek() is not None and self._peek().kind == "COMMA":
                self._consume()
                args.append(self.parse_expr())
        self._expect("RPAREN", "')'")
        return Call(name=ident.lexeme, args=tuple(args))

    def _parse_path_from_already_consumed(self, first: _Token) -> Path:
        """Same as ``_parse_path`` but ``first`` is already
        consumed (used when we discovered the path inside
        a value-or-path expression)."""
        return self._parse_path(first.lexeme)

    # value = number | string | "true" | "false" | "null"
    def _parse_value(self) -> Literal:
        tok = self._peek()
        if tok is None:
            raise ConcordoSyntaxError(
                expression=self._expression,
                line=len(self._expression.splitlines()),
                column=len(self._expression),
                expected="literal value",
                found="end of input",
            )
        if tok.kind == "NUMBER":
            self._consume()
            return Literal(value=_parse_number_literal(tok.lexeme))
        if tok.kind == "STRING":
            self._consume()
            return Literal(value=_parse_string_literal(tok.lexeme))
        if tok.kind == "IDENT" and tok.lexeme in ("true", "false", "null"):
            self._consume()
            return Literal(
                value=(
                    True
                    if tok.lexeme == "true"
                    else (False if tok.lexeme == "false" else None)
                )
            )
        raise ConcordoSyntaxError(
            expression=self._expression,
            line=tok.line,
            column=tok.column,
            expected=("literal value (number, string, true, false, null)"),
            found=f"{tok.kind}({tok.lexeme!r})",
        )


def _parse_string_literal(text: str) -> str:
    """Strip the surrounding quotes and unescape escapes."""
    # text starts with ' or "
    quote = text[0]
    inner = text[1:-1]
    # Support the same escapes Python recognises for
    # simple literals: \\ \' \" \n \r \t \0 etc. We do NOT
    # process Unicode or octal escapes — those would invite
    # parser confusion.
    return inner.replace(f"\\{quote}", quote).replace("\\\\", "\\")


def _parse_number_literal(text: str) -> int | float:
    """Parse an integer or float literal."""
    if "." in text:
        return float(text)
    return int(text)


def parse_expression(expression: str) -> Expr:
    """Parse ``expression`` into an ``Expr`` AST.

    Raises ``ConcordoSyntaxError`` on a malformed expression.
    """
    expression = expression.strip()
    if not expression:
        raise ConcordoSyntaxError(
            expression=expression,
            line=1,
            column=1,
            expected="expression",
            found="end of input",
            hint="empty expression",
        )
    tokens = _tokenize(expression)
    parser = _Parser(expression, tokens)
    ast = parser.parse_expr()
    # Reject trailing tokens (catches things like
    # ``a == b garbage``).
    if parser._i < len(tokens):
        tok = tokens[parser._i]
        raise ConcordoSyntaxError(
            expression=expression,
            line=tok.line,
            column=tok.column,
            expected="end of input",
            found=f"{tok.kind}({tok.lexeme!r})",
            hint="remove trailing tokens",
        )
    return ast


def is_pure_name(expression: str) -> str | None:
    """Return the spec name if ``expression`` is a pure
    name lookup; ``None`` otherwise.

    A pure name lookup is a single identifier with no
    operators, paths, or parens (matches the
    "name lookup" form in the module docstring).
    """
    expression = expression.strip()
    if not expression:
        return None
    tokens = _tokenize(expression)
    if len(tokens) != 1 or tokens[0].kind != "IDENT":
        return None
    return tokens[0].lexeme


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


# Built-in spec bindings. The mini-language function name
# maps to a factory that takes positional args and
# returns a Specification instance. The evaluator
# instantiates the spec at evaluation time, using the
# StepContext.
#
# The factories here return *new spec instances* per
# evaluation so that spec arguments (e.g. ``name``) are
# bound correctly. Specs are cheap; the cost is negligible.
BUILTIN_SPECS: dict[str, Callable[..., "Specification"]] = {}


def _register_builtins() -> None:
    """Lazy registration of the built-in spec factories.
    Imports the spec classes lazily to avoid a circular
    import (specs.py imports ``Composable`` from base.py
    which we are defining)."""
    global BUILTIN_SPECS
    if BUILTIN_SPECS:
        return  # already populated
    from .specs import (
        ContinuityToolUsed,
        DomainStateIs,
        ProfileTierIs,
        StepCompleted,
        StepFailed,
        StepTimedOut,
    )

    BUILTIN_SPECS.update(
        {
            "step_completed": lambda name="": StepCompleted(step_name=name),
            "step_failed": lambda name="": StepFailed(step_name=name),
            "step_timed_out": lambda name="": StepTimedOut(step_name=name),
            "domain_state_is": (
                lambda field="", value="": DomainStateIs(field=field, value=value)
            ),
            "profile_tier_is": lambda tier="": ProfileTierIs(tier=tier),
            "continuity_tool_used": (
                lambda tool_name="": ContinuityToolUsed(tool_name=tool_name)
            ),
        }
    )


def _call_builtin(name: str, args: tuple[Any, ...], ctx: "StepContext") -> bool:
    """Instantiate a built-in spec with the parsed args
    and evaluate against the context."""
    _register_builtins()
    factory = BUILTIN_SPECS.get(name)
    if factory is None:
        raise ValueError(
            f"unknown built-in spec: {name!r}. Available: {sorted(BUILTIN_SPECS)}"
        )
    # All built-in specs take string args today; coerce
    # other Literal types to str for compatibility.
    spec = factory(
        *(str(a) if isinstance(a, (str, int, float, bool)) else a for a in args)
    )
    return spec.is_satisfied_by(ctx)


def _resolve_path(path: Path, ctx: "StepContext") -> Any:
    """Resolve a path expression against the step context.

    Returns ``None`` for missing keys (graceful failure,
    per docs/concordos-bundle-spec.md §2.4).
    """
    if path.scope == "event.data":
        if ctx.trigger_data is None or not path.tail:
            return None
        cur: Any = dict(ctx.trigger_data)
        for field in path.tail:
            if isinstance(cur, Mapping):
                cur = cur.get(field)
            else:
                return None
            if cur is None:
                return None
        return cur
    elif path.scope == "steps":
        if not path.tail:
            return None
        step_name = path.tail[0]
        cur = dict(ctx.step_results).get(step_name)
        if cur is None:
            return None
        # ``steps.<name>.output.<field>`` skips the ``output``
        # tail token; ``steps.<name>.<direct field>`` is also
        # supported for convenience.
        start = 2 if len(path.tail) >= 2 and path.tail[1] == "output" else 1
        cur = cur
        for field in path.tail[start:]:
            if isinstance(cur, Mapping):
                cur = cur.get(field)
            else:
                return None
            if cur is None:
                return None
        return cur
    elif path.scope == "agent":
        if ctx.domain is None or not path.tail:
            return None
        cur = ctx.domain
        # agent.<field> uses dotted attribute access, not
        # Mapping access — DomainComponent is a dataclass.
        for field in path.tail:
            cur = getattr(cur, field, None)
            if cur is None:
                return None
        return cur
    elif path.scope == "now":
        if not path.tail:
            return ctx.now
        # now.<attr> — limited to a few safe attributes.
        attr = path.tail[0]
        dt = ctx.now
        safe = {"year", "month", "day", "hour", "minute", "second", "weekday"}
        if attr not in safe:
            return None
        return getattr(dt, attr, None)
    return None  # pragma: no cover (unreachable scope)


def _compare(lhs: Any, op: str, rhs: Any) -> bool:
    """Evaluate a single comparison with type coercion.

    Per docs/concordos-bundle-spec.md §2.4:

      number vs number    numeric
      string vs string    lexicographic
      bool vs bool       strict (no coercion)
      anything vs null   False unless both null
      path missing      False
    """
    if lhs is None or rhs is None:
        # Equality with null: both null ⇒ True; else False.
        if op == "==":
            return lhs is None and rhs is None
        if op == "!=":
            return not (lhs is None and rhs is None)
        return False
    # Same-type comparisons.
    if isinstance(lhs, bool) and isinstance(rhs, bool):
        if op == "==":
            return lhs == rhs
        if op == "!=":
            return lhs != rhs
        return False  # bool comparison: only == / !=
    if isinstance(lhs, (int, float)) and isinstance(rhs, (int, float)):
        return _compare_numbers(lhs, op, rhs)
    if isinstance(lhs, str) and isinstance(rhs, str):
        return _compare_strings(lhs, op, rhs)
    # Mixed types: only == / !=.
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    return False


def _compare_numbers(lhs: Any, op: str, rhs: Any) -> bool:
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    return False


def _compare_strings(lhs: Any, op: str, rhs: Any) -> bool:
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    return False


def evaluate(expr: Expr, ctx: "StepContext") -> bool:
    """Evaluate the parsed AST against a ``StepContext``.

    Pure: no I/O, no mutation, no event emission. The
    result is a bool.
    """
    if isinstance(expr, NameLookup):
        # Name lookups are resolved by the loader via
        # SpecRegistry. By the time we get here, a
        # NameLookup should not appear (the loader
        # converts it to the resolved spec). If it does,
        # we treat it as "no match" — fail safe.
        raise ValueError(
            "NameLookup AST should be resolved by the "
            "loader before evaluation; got "
            f"{expr.name!r} unresolved"
        )
    if isinstance(expr, Or):
        return evaluate(expr.left, ctx) or evaluate(expr.right, ctx)
    if isinstance(expr, And):
        return evaluate(expr.left, ctx) and evaluate(expr.right, ctx)
    if isinstance(expr, Not):
        return not evaluate(expr.operand, ctx)
    if isinstance(expr, Compare):
        lhs = _resolve_path(expr.path, ctx)
        # RHS: Literal | Path | NameLookup. The path/value
        # distinction was elided at parse time — anything
        # that survived _parse_value_or_path is one of
        # those. Resolve paths and name lookups to actual
        # values; literals carry their value directly.
        if isinstance(expr.value, Literal):
            rhs: Any = expr.value.value
        elif isinstance(expr.value, Path):
            rhs = _resolve_path(expr.value, ctx)
        elif isinstance(expr.value, NameLookup):
            # A bare name on the RHS: it should have been
            # a Path (with scope=""). If we see NameLookup
            # here, treat it as no-match (fail safe).
            rhs = None
        else:
            rhs = None
        return _compare(lhs, expr.op, rhs)
    if isinstance(expr, Call):
        args = tuple(
            _literal_value(a, ctx) if isinstance(a, Literal) else evaluate(a, ctx)
            for a in expr.args
        )
        return _call_builtin(expr.name, args, ctx)
    if isinstance(expr, Path):
        return bool(_resolve_path(expr, ctx))
    if isinstance(expr, Literal):
        return bool(expr.value)
    raise ValueError(f"unknown AST node: {type(expr).__name__}")  # pragma: no cover


def _literal_value(lit: Literal, ctx: "StepContext") -> Any:
    """Resolve a Literal node against the StepContext.

    Used by ``Call`` to coerce string/number literals into
    the spec factory's typed positional arguments.
    """
    return lit.value
