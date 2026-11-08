# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Debt detectors — the inventory behind the ``fix-on-touch`` policy.

The inventory for the conditional regime. It never fails on the
existence of debt; it reports it, and compares it with a recorded
baseline so a **regression** (new debt) can be detected.

See ``.agents/rules/fix_on_touch.md`` for the policy and
``scripts/debt.py`` for the command-line interface.

Calibration
-----------
Every detector was **calibrated against the current tree** before it
was written. A naive detector for ``except Exception`` reports 25
framework sites; classifying each showed that 13 carry an explicit
justification, 7 are the ADR-077 typed-translation boundary, and 8 are
deliberate last-resort handlers in worker loops. The naive rule was
therefore unreachable, and the rule was rewritten to measure what is
reachable. The same calibration produced the exclusions documented on
each detector.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tests.architecture._collectors import (
    ADRS_ROOT,
    DOCS_ROOT,
    PROJECT_ROOT,
    SRC_ROOT,
)

#: Where the recorded debt lives.
BASELINE_PATH: Path = PROJECT_ROOT / ".debt-baseline.json"

#: Marker that documents an intentional broad ``except``.
_BLESSED_MARKER: str = "noqa: BLE001"

#: A Markdown link target, e.g. ``[label](target)``.
_MARKDOWN_LINK: re.Pattern[str] = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

#: Schemes that are not repository-relative links.
_EXTERNAL_SCHEMES: tuple[str, ...] = ("http://", "https://", "mailto:", "file://")

#: Framework-tier packages (the tiers the type-discipline skill
#: applies to in full).
FRAMEWORK_PACKAGES: frozenset[str] = frozenset(
    {
        "core",
        "tools",
        "infra",
        "stream",
        "security",
        "runner",
        "resilience",
        "concordos",
    }
)


@dataclass(frozen=True, slots=True)
class Debt:
    """One unit of inventoried technical debt.

    Attributes:
        path: Repository-relative path of the offending file.
        kind: Stable machine-readable debt kind (see ``DEBT_KINDS``).
        detail: Stable human-readable discriminator. Two debts of the
            same kind in the same file must differ in ``detail`` for
            the baseline to count them separately.
        line: 1-based line number, for navigation only. It is **not**
            part of the debt identity — line numbers shift on every
            unrelated edit.
    """

    path: str
    kind: str
    detail: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str]:
        """Stable key of the debt, used to compare against a baseline."""
        return (self.path, self.kind, self.detail)


#: Human-readable description of every debt kind.
DEBT_KINDS: dict[str, str] = {
    "broad-except": (
        "broad 'except Exception' that neither re-raises a typed error "
        "nor carries an explicit justification"
    ),
    "print-call": "print() in production code outside the CLI user interface",
    "dict-str-any": "dict[str, Any] annotation instead of dict[str, JsonValue]",
    "type-any-key": "type[Any] in a mapping key (ADR-080 narrows it to 'type')",
    "bare-object": "bare 'object' annotation (banned in framework code)",
    "broken-doc-link": "relative Markdown link whose target does not exist",
    "duplicate-adr-number": "two ADRs share the same number",
}


# ---------------------------------------------------------------------------
# Code debt detectors
# ---------------------------------------------------------------------------


def _is_dict_str_any(annotation: ast.expr) -> bool:
    """True for ``dict[str, Any]``."""
    if not isinstance(annotation, ast.Subscript):
        return False
    if getattr(annotation.value, "id", "") != "dict":
        return False
    if not isinstance(annotation.slice, ast.Tuple):
        return False
    elements = annotation.slice.elts
    if len(elements) != 2:
        return False
    key, value = elements
    return getattr(key, "id", "") == "str" and getattr(value, "id", "") == "Any"


def _mapping_key_has_type_any(annotation: ast.expr) -> bool:
    """True for a mapping whose **key** embeds ``type[Any]``.

    This is the ADR-080 shape: the ECS bag key was narrowed from
    ``Mapping[str | type[Any], Any]`` to ``Mapping[str | type, Any]``.

    A **standalone** ``type[Any]`` (e.g. ``def component_meta(
    t: type[Any])``) is deliberately *not* flagged: it is a class
    reference of unknown type, not the bag key. Only the mapping-key
    shape is a violation.
    """
    if not isinstance(annotation, ast.Subscript):
        return False
    container = getattr(annotation.value, "id", "")
    if container not in ("dict", "Mapping", "MutableMapping"):
        return False
    return "type[Any]" in ast.unparse(annotation.slice)


def _annotation_nodes(tree: ast.Module) -> list[tuple[ast.expr, int]]:
    """Every annotation in the module, paired with its line number."""
    found: list[tuple[ast.expr, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            found.append((node.annotation, node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in (*node.args.args, *node.args.kwonlyargs):
                if arg.annotation is not None:
                    found.append((arg.annotation, arg.lineno))
            if node.returns is not None:
                found.append((node.returns, node.lineno))
    return found


def _collect_broad_except(path: Path, tree: ast.Module, source: str) -> list[Debt]:
    """Detect broad ``except`` handlers that are not justified.

    Exclusions (each one is a calibration result, not a guess):

    - ``# noqa: BLE001`` — the author wrote the justification inline.
    - The handler re-raises a typed error — this is the ADR-077
      translation boundary, where catching broadly is the *point*.
    """
    debts: list[Debt] = []
    lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            caught = "bare except"
        else:
            caught = ast.unparse(node.type)
            if caught not in ("Exception", "BaseException"):
                continue
        segment = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        if _BLESSED_MARKER in segment:
            continue
        re_raises_typed = any(
            isinstance(child, ast.Raise)
            and child.exc is not None
            and (not isinstance(child.exc, ast.Name) or child.exc.id.endswith("Error"))
            for child in ast.walk(node)
        )
        if re_raises_typed:
            continue
        debts.append(
            Debt(
                path=path.relative_to(PROJECT_ROOT).as_posix(),
                kind="broad-except",
                detail=caught,
                line=node.lineno,
            )
        )
    return debts


def _collect_print_calls(path: Path, tree: ast.Module) -> list[Debt]:
    """Detect ``print()`` in production code.

    Exclusion: the ``cli`` package. A CLI command's ``print()`` *is*
    its user interface — stdout is the intended sink there.
    """
    if path.is_relative_to(SRC_ROOT / "cli"):
        return []
    debts: list[Debt] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            debts.append(
                Debt(
                    path=path.relative_to(PROJECT_ROOT).as_posix(),
                    kind="print-call",
                    detail="print()",
                    line=node.lineno,
                )
            )
    return debts


def _collect_type_annotations(path: Path, tree: ast.Module) -> list[Debt]:
    """Detect ``dict[str, Any]``, ``type[Any]`` bag keys and bare ``object``.

    Exclusions (calibration results):

    - ``core/_typing.py`` defines the ``JsonValue`` / ``ComponentT``
      primitives. Their own union base is legitimately ``object``.
    - ``dict[str, Any]`` on a binding literally named ``data`` in
      framework code is already a fail-fast violation of
      ``design_gate.py`` rule ``KNT-TYPE-002``; the two gates are kept
      disjoint so that a single violation is reported once.
    """
    relative_path = path.relative_to(PROJECT_ROOT).as_posix()
    if relative_path == "src/kntgraph/core/_typing.py":
        return []
    debts: list[Debt] = []
    for annotation, line in _annotation_nodes(tree):
        if _is_dict_str_any(annotation):
            debts.append(
                Debt(
                    path=relative_path,
                    kind="dict-str-any",
                    detail="dict[str, Any]",
                    line=line,
                )
            )
        elif _mapping_key_has_type_any(annotation):
            debts.append(
                Debt(
                    path=relative_path,
                    kind="type-any-key",
                    detail="type[Any] in mapping key",
                    line=line,
                )
            )
        elif ast.unparse(annotation).strip() == "object":
            debts.append(
                Debt(
                    path=relative_path,
                    kind="bare-object",
                    detail="object",
                    line=line,
                )
            )
    return debts


def collect_code_debts() -> list[Debt]:
    """Scan ``src/kntgraph`` and return every unit of code debt."""
    debts: list[Debt] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        debts.extend(_collect_broad_except(path, tree, source))
        debts.extend(_collect_print_calls(path, tree))
        debts.extend(_collect_type_annotations(path, tree))
    return debts


# ---------------------------------------------------------------------------
# Documentation debt detectors
# ---------------------------------------------------------------------------


def _markdown_targets(text: str) -> list[str]:
    """Every relative Markdown link target in the text.

    Placeholders (an angle-bracket slot, or the ``XXX`` marker) are
    skipped: a template shows the *shape* of a link, not a link.
    """
    targets: list[str] = []
    for match in _MARKDOWN_LINK.finditer(text):
        raw = match.group(1).split("#")[0].strip()
        if "<" in raw or ">" in raw or "XXX" in raw:
            continue
        target = raw.strip("<>")
        if not target or target.startswith(_EXTERNAL_SCHEMES):
            continue
        targets.append(target)
    return targets


def collect_doc_debts() -> list[Debt]:
    """Scan ``docs/`` and return every broken relative Markdown link.

    Exclusions:

    - ``docs/adr-history/`` holds an archived copy of a superseded ADR.
      It is a historical record, not a normative surface, so its links
      are not invariants.
    - The normative surface (``.agents/`` and the repository-root
      documents) is enforced fail-fast by
      ``tests/architecture/test_docs_consistency.py``. This detector
      only inventories the residual debt in ``docs/``.
    """
    debts: list[Debt] = []
    if not DOCS_ROOT.is_dir():
        return debts
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        relative_path = path.relative_to(PROJECT_ROOT)
        if "adr-history" in relative_path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for target in _markdown_targets(text):
            if (path.parent / target).resolve().exists():
                continue
            debts.append(
                Debt(
                    path=relative_path.as_posix(),
                    kind="broken-doc-link",
                    detail=target,
                    line=0,
                )
            )
    return debts


def collect_duplicate_adr_numbers() -> list[Debt]:
    """Return one debt per ADR number claimed by more than one file.

    ADR numbers are the identifiers every cross-reference uses. Two
    ADRs sharing a number make those references ambiguous, so the
    ambiguity propagates — this is structural debt.
    """
    if not ADRS_ROOT.is_dir():
        return []
    by_number: dict[str, list[str]] = {}
    for path in sorted(ADRS_ROOT.glob("ADR-*.md")):
        number = path.name.split("-")[1]
        by_number.setdefault(number, []).append(path.name)
    debts: list[Debt] = []
    for number, names in sorted(by_number.items()):
        if len(names) < 2:
            continue
        for name in names:
            debts.append(
                Debt(
                    path=f"ADRs/{name}",
                    kind="duplicate-adr-number",
                    detail=f"ADR-{number}",
                    line=0,
                )
            )
    return debts


def collect_debts() -> list[Debt]:
    """Every unit of inventoried debt: code, documentation and ADRs."""
    return collect_code_debts() + collect_doc_debts() + collect_duplicate_adr_numbers()


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def load_baseline() -> Counter[tuple[str, str, str]]:
    """Read the recorded debt identities from the baseline file."""
    if not BASELINE_PATH.exists():
        return Counter()
    payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    items = payload.get("items", [])
    return Counter((item["path"], item["kind"], item["detail"]) for item in items)


def write_baseline(debts: list[Debt]) -> None:
    """Record the current debt as the new baseline."""
    items: list[dict[str, str | int]] = []
    for debt in sorted(debts, key=lambda item: (item.path, item.kind, item.detail)):
        items.append(
            {
                "path": debt.path,
                "kind": debt.kind,
                "detail": debt.detail,
                "line": debt.line,
            }
        )
    payload: dict[str, str | int | list[dict[str, str | int]]] = {
        "version": 1,
        "description": (
            "Known technical debt, inventoried by tests/architecture/_debt.py. "
            "The debt check fails only on a REGRESSION (debt not present in "
            "this file). Regenerate with "
            "'uv run python scripts/debt.py --update-baseline'."
        ),
        "items": items,
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def new_debt(baseline: Counter[tuple[str, str, str]]) -> list[Debt]:
    """Return the debts that the baseline does not cover."""
    recorded = baseline.copy()
    fresh: list[Debt] = []
    for debt in collect_debts():
        if recorded[debt.identity] > 0:
            recorded[debt.identity] -= 1
        else:
            fresh.append(debt)
    return fresh
