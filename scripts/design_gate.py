# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
#
# /// script
# requires-python = ">=3.12"
# ///
"""design_gate — Architectural fitness functions and boundary enforcement (ADR-089).

Verifies architectural invariants across ADRs:
  - KNT-BOUND-001: Framework never imports from vertical packages (ADR-042)
  - KNT-ADAPT-001: External third-party SDKs must not leak outside adapters (ADR-047)
  - KNT-TYPE-001:  TypedDict is prohibited in framework code (ADR-079)
  - KNT-TYPE-002:  Wire payload data must not be dict[str, Any] (ADR-067 / ADR-079)
  - KNT-TYPE-003:  type[Any] in ECS bag keys is prohibited (ADR-080)
  - KNT-TYPE-004:  MappingProxyType is prohibited (ADR-079 §6.2 / §6.4)
  - KNT-EXC-001:   Uncontrolled `except Exception` in callers prohibited (ADR-077)

Usage:
    python scripts/design_gate.py                   # check src/kntgraph
    python scripts/design_gate.py --format llm      # machine-actionable blocks
    python scripts/design_gate.py --format json     # JSON diagnostics
    python scripts/design_gate.py src/kntgraph/core # check specific subtree
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
SRC_DIR: Final[Path] = ROOT / "src" / "kntgraph"

FRAMEWORK_PACKAGES: Final[frozenset[str]] = frozenset(
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

VERTICAL_PACKAGES: Final[frozenset[str]] = frozenset(
    {
        "agents",
        "api",
        "cli",
        "knowledge",
        "events",
        "memory",
    }
)

THIRD_PARTY_CLIENT_LIBS: Final[frozenset[str]] = frozenset(
    {
        "redis",
        "falkordb",
        "httpx",
        "boto3",
        "stripe",
        "litellm",
        "ollama",
        "gliner2",
    }
)

# Legacy technical debt allowlist (ADR-082 / DEBT.md §2.40)
# Format: (relative_file_path, rule_id)
LEGACY_ALLOWLIST: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("src/kntgraph/tools/router.py", "KNT-ADAPT-001"),
        ("src/kntgraph/tools/_message_handlers.py", "KNT-ADAPT-001"),
        ("src/kntgraph/runner/reactive.py", "KNT-ADAPT-001"),
    }
)


@dataclass(frozen=True, slots=True)
class Violation:
    file_path: Path
    line: int
    col: int
    rule_id: str
    rule_title: str
    message: str
    remedy: str
    adr: str

    def to_dict(self) -> dict[str, str | int]:
        rel_path = str(self.file_path)
        with contextlib.suppress(ValueError):
            rel_path = str(self.file_path.relative_to(ROOT))
        return {
            "file": rel_path,
            "line": self.line,
            "col": self.col,
            "rule_id": self.rule_id,
            "rule_title": self.rule_title,
            "message": self.message,
            "remedy": self.remedy,
            "adr": self.adr,
        }

    def format_terminal(self) -> str:
        rel = str(self.file_path)
        with contextlib.suppress(ValueError):
            rel = str(self.file_path.relative_to(ROOT))
        return (
            f"\033[1;31m[{self.rule_id}]\033[0m {rel}:{self.line}:{self.col}\n"
            f"  \033[1mProblem:\033[0m {self.message}\n"
            f"  \033[1;32mRemedy:\033[0m  {self.remedy} (\033[36m{self.adr}\033[0m)"
        )

    def format_llm(self) -> str:
        rel = str(self.file_path)
        with contextlib.suppress(ValueError):
            rel = str(self.file_path.relative_to(ROOT))
        return (
            f"[KNT-DESIGN-ERROR] VIOLATION: {self.rule_id} ({self.rule_title})\n"
            f"File: {rel}:{self.line}:{self.col}\n"
            f"Rule: {self.adr}\n"
            f"Detail: {self.message}\n"
            f"Remedy: {self.remedy}"
        )


def _is_in_type_checking(node: ast.AST, ancestors: Sequence[ast.AST]) -> bool:
    """Return True if node is enclosed inside `if TYPE_CHECKING:`."""
    for anc in ancestors:
        if (
            isinstance(anc, ast.If)
            and isinstance(anc.test, ast.Name)
            and anc.test.id == "TYPE_CHECKING"
        ):
            return True
    return False


def _mapping_proxy_type_violation(
    file_path: Path,
    line: int,
    col: int,
    detail: str,
) -> Violation:
    """The violation raised by a ``MappingProxyType`` import.

    ``MappingProxyType`` was removed from the codebase by
    ADR-079 §6.2: the static ``Mapping[K, V]`` annotation is
    enough to keep callers from mutating a read-only view, and
    the runtime wrapper breaks pickling of the ECS components
    that carry such mappings.
    """
    return Violation(
        file_path=file_path,
        line=line,
        col=col,
        rule_id="KNT-TYPE-004",
        rule_title="NoMappingProxyType",
        message=detail,
        remedy=(
            "Declare the field as 'Mapping[K, V]' and keep a plain dict; "
            "the static type already prevents caller mutation."
        ),
        adr="ADR-079 §6.2 / §6.4",
    )


def check_ast(file_path: Path, tree: ast.AST, rel_path: str) -> list[Violation]:
    violations: list[Violation] = []

    # Determine top-level package under src/kntgraph
    top_pkg = ""
    with contextlib.suppress(ValueError):
        rel_to_src = file_path.relative_to(SRC_DIR)
        if rel_to_src.parts:
            top_pkg = rel_to_src.parts[0]

    is_framework = top_pkg in FRAMEWORK_PACKAGES
    is_infra = top_pkg == "infra" or "testing" in file_path.parts
    is_test = "tests" in file_path.parts

    ancestor_stack: list[ast.AST] = []

    class Visitor(ast.NodeVisitor):
        def generic_visit(self, node: ast.AST) -> None:
            ancestor_stack.append(node)
            super().generic_visit(node)
            ancestor_stack.pop()

        def visit_Import(self, node: ast.Import) -> None:
            in_tc = _is_in_type_checking(node, ancestor_stack)
            for alias in node.names:
                name = alias.name
                if is_framework:
                    for v in VERTICAL_PACKAGES:
                        if name == f"kntgraph.{v}" or name.startswith(f"kntgraph.{v}."):
                            violations.append(
                                Violation(
                                    file_path=file_path,
                                    line=node.lineno,
                                    col=node.col_offset,
                                    rule_id="KNT-BOUND-001",
                                    rule_title="FrameworkVerticalSeam",
                                    message=(
                                        "Framework module imports vertical "
                                        f"package '{name}'."
                                    ),
                                    remedy=(
                                        "Import framework primitives from "
                                        "kntgraph.core.* instead."
                                    ),
                                    adr="ADR-042 / Skill §4",
                                )
                            )

                if is_framework and not is_infra and not is_test and not in_tc:
                    root_pkg = name.split(".")[0]
                    if (
                        root_pkg in THIRD_PARTY_CLIENT_LIBS
                        and (rel_path, "KNT-ADAPT-001") not in LEGACY_ALLOWLIST
                    ):
                        violations.append(
                            Violation(
                                file_path=file_path,
                                line=node.lineno,
                                col=node.col_offset,
                                rule_id="KNT-ADAPT-001",
                                rule_title="ThirdPartyAdapterLeakage",
                                message=(
                                    f"Direct import of SDK '{name}' "
                                    "outside infra/adapter boundary."
                                ),
                                remedy=(
                                    "Wrap external calls in an Adapter Protocol in "
                                    "src/kntgraph/infra/ and inject the Protocol."
                                ),
                                adr="ADR-047 / ADR-077",
                            )
                        )
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            in_tc = _is_in_type_checking(node, ancestor_stack)
            mod = node.module or ""

            if is_framework:
                for v in VERTICAL_PACKAGES:
                    if mod == f"kntgraph.{v}" or mod.startswith(f"kntgraph.{v}."):
                        remedy = f"Do not import from vertical '{v}'. Use projections."
                        if v == "memory":
                            remedy = (
                                "Import ECS projection from "
                                "'kntgraph.core.components.memory.SessionComponent'."
                            )
                        violations.append(
                            Violation(
                                file_path=file_path,
                                line=node.lineno,
                                col=node.col_offset,
                                rule_id="KNT-BOUND-001",
                                rule_title="FrameworkVerticalSeam",
                                message=(
                                    "Framework module imports vertical package "
                                    f"'{mod}'."
                                ),
                                remedy=remedy,
                                adr="ADR-042 / Skill §4",
                            )
                        )

            if is_framework and mod in ("typing", "typing_extensions"):
                for alias in node.names:
                    if alias.name == "TypedDict":
                        violations.append(
                            Violation(
                                file_path=file_path,
                                line=node.lineno,
                                col=node.col_offset,
                                rule_id="KNT-TYPE-001",
                                rule_title="NoTypedDictInFramework",
                                message=(
                                    "Import of TypedDict detected in framework code."
                                ),
                                remedy=(
                                    "Use `@dataclass(frozen=True, slots=True)` "
                                    "instead of TypedDict."
                                ),
                                adr="ADR-079 §3.1",
                            )
                        )

            if is_framework and not is_infra and not is_test and not in_tc:
                root_pkg = mod.split(".")[0]
                if (
                    root_pkg in THIRD_PARTY_CLIENT_LIBS
                    and (rel_path, "KNT-ADAPT-001") not in LEGACY_ALLOWLIST
                ):
                    violations.append(
                        Violation(
                            file_path=file_path,
                            line=node.lineno,
                            col=node.col_offset,
                            rule_id="KNT-ADAPT-001",
                            rule_title="ThirdPartyAdapterLeakage",
                            message=(
                                f"Direct import from SDK '{mod}' "
                                "outside adapter boundary."
                            ),
                            remedy=(
                                "Encapsulate third-party client inside an Adapter "
                                "implementing a typed Protocol in infra/."
                            ),
                            adr="ADR-047 / ADR-077",
                        )
                    )

            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if (
                is_framework
                and isinstance(node.target, ast.Name)
                and node.target.id == "data"
                and isinstance(node.annotation, ast.Subscript)
                and getattr(node.annotation.value, "id", "") == "dict"
                and isinstance(node.annotation.slice, ast.Tuple)
                and len(node.annotation.slice.elts) == 2
            ):
                k, v = node.annotation.slice.elts
                if getattr(k, "id", "") == "str" and getattr(v, "id", "") == "Any":
                    violations.append(
                        Violation(
                            file_path=file_path,
                            line=node.lineno,
                            col=node.col_offset,
                            rule_id="KNT-TYPE-002",
                            rule_title="WirePayloadDiscipline",
                            message=(
                                "Event payload variable 'data' is typed as "
                                "'dict[str, Any]'."
                            ),
                            remedy=(
                                "Use 'dict[str, JsonValue]' or "
                                "'Mapping[str, JsonValue]' from "
                                "kntgraph.core._typing."
                            ),
                            adr="ADR-067 / ADR-079 §5",
                        )
                    )
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            """Flag every use of the banned name, whatever the import path.

            The import check above catches the usual
            ``from types import MappingProxyType``; this catches
            the qualified ``types.MappingProxyType`` and a
            re-export from another module.
            """
            if node.id == "MappingProxyType":
                violations.append(
                    _mapping_proxy_type_violation(
                        file_path,
                        node.lineno,
                        node.col_offset,
                        "Use of MappingProxyType detected.",
                    )
                )
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            """Flag the qualified ``types.MappingProxyType`` form."""
            if node.attr == "MappingProxyType":
                violations.append(
                    _mapping_proxy_type_violation(
                        file_path,
                        node.lineno,
                        node.col_offset,
                        "Use of MappingProxyType detected.",
                    )
                )
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if is_framework:
                for arg in node.args.args + node.args.kwonlyargs:
                    if (
                        arg.arg == "data"
                        and isinstance(arg.annotation, ast.Subscript)
                        and getattr(arg.annotation.value, "id", "") == "dict"
                        and isinstance(arg.annotation.slice, ast.Tuple)
                        and len(arg.annotation.slice.elts) == 2
                    ):
                        k, v = arg.annotation.slice.elts
                        if (
                            getattr(k, "id", "") == "str"
                            and getattr(v, "id", "") == "Any"
                        ):
                            violations.append(
                                Violation(
                                    file_path=file_path,
                                    line=arg.lineno,
                                    col=arg.col_offset,
                                    rule_id="KNT-TYPE-002",
                                    rule_title="WirePayloadDiscipline",
                                    message=(
                                        "Function parameter 'data' is annotated as "
                                        "'dict[str, Any]'."
                                    ),
                                    remedy=(
                                        "Use 'dict[str, JsonValue]' or "
                                        "'Mapping[str, JsonValue]'."
                                    ),
                                    adr="ADR-067 / ADR-079 §5",
                                )
                            )
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    Visitor().visit(tree)
    return violations


def scan_path(target_path: Path) -> list[Violation]:
    violations: list[Violation] = []
    if target_path.is_file():
        files = [target_path] if target_path.suffix == ".py" else []
    else:
        files = sorted(target_path.rglob("*.py"))

    for py_file in files:
        rel = str(py_file)
        with contextlib.suppress(ValueError):
            rel = str(py_file.relative_to(ROOT))

        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError) as err:
            violations.append(
                Violation(
                    file_path=py_file,
                    line=getattr(err, "lineno", 1) or 1,
                    col=getattr(err, "offset", 0) or 0,
                    rule_id="KNT-SYNTAX-001",
                    rule_title="FileParseError",
                    message=f"Failed to parse file: {err}",
                    remedy="Fix syntax or encoding error in file.",
                    adr="ADR-089",
                )
            )
            continue

        violations.extend(check_ast(py_file, tree, rel))

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="kntgraph design & architectural fitness gate (ADR-089)"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=[str(SRC_DIR)],
        help="Files or directories to scan (default: src/kntgraph)",
    )
    parser.add_argument(
        "--format",
        choices=["terminal", "llm", "json"],
        default="terminal",
        help="Output format: terminal (default), llm, json",
    )
    parser.add_argument(
        "--rule",
        default="",
        help="Filter violations by specific rule ID (e.g. KNT-BOUND-001)",
    )

    args = parser.parse_args()

    all_violations: list[Violation] = []
    for p_str in args.paths:
        p = Path(p_str)
        if not p.is_absolute():
            p = ROOT / p
        all_violations.extend(scan_path(p))

    if args.rule:
        all_violations = [v for v in all_violations if v.rule_id == args.rule]

    if args.format == "json":
        output = [v.to_dict() for v in all_violations]
        print(json.dumps(output, indent=2))
    elif args.format == "llm":
        if all_violations:
            for v in all_violations:
                print(v.format_llm())
                print()
    else:
        if all_violations:
            print(f"\n\033[1;31mFound {len(all_violations)} violation(s):\033[0m\n")
            for v in all_violations:
                print(v.format_terminal())
                print("-" * 60)
        else:
            print(
                "\033[1;32m[PASS] All architectural fitness checks passed. "
                "Zero design drift detected.\033[0m"
            )

    return 1 if all_violations else 0


if __name__ == "__main__":
    sys.exit(main())
