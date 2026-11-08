# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Shared mechanics for the architecture checks.

Each file in ``tests/architecture/`` asserts **one** architectural
invariant and contains **only the rule** — never the parsing
mechanics. The mechanics live here so that a check reads like a
specification.

This split is what makes the checks legible as rules: the collector
answers "what does the code look like?", the check answers "is that
allowed?".

References:
  - ``.agents/skills/kntgraph-harness/SKILL.md`` — the rules.
  - ``docs/tasks/README.md`` — the Definition-of-Done contract.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

#: Repository root (the parent of ``tests/``).
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

#: Production source root.
SRC_ROOT: Path = PROJECT_ROOT / "src" / "kntgraph"

#: Test root.
TESTS_ROOT: Path = PROJECT_ROOT / "tests"

#: Architecture Decision Records.
ADRS_ROOT: Path = PROJECT_ROOT / "ADRs"

#: Documentation root.
DOCS_ROOT: Path = PROJECT_ROOT / "docs"

#: Execution plans (Level 4 of the harness).
TASKS_DIR: Path = DOCS_ROOT / "tasks"

#: Agent skills — the normative surface the agent reads.
SKILLS_ROOT: Path = PROJECT_ROOT / ".agents" / "skills"

#: Transversal policies (fix-on-touch, migrate-on-touch).
RULES_ROOT: Path = PROJECT_ROOT / ".agents" / "rules"

#: A Markdown link target, e.g. ``[label](target)``.
MARKDOWN_LINK: re.Pattern[str] = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

#: Schemes that are not repository-relative references.
EXTERNAL_SCHEMES: tuple[str, ...] = (
    "http://",
    "https://",
    "mailto:",
    "file://",
)


def relative(path: Path) -> str:
    """Return the repository-relative form of ``path``.

    Used in failure messages so that they are stable regardless of
    where the suite is invoked from.
    """
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def iter_source_files(*, include_tests: bool = False) -> Iterator[Path]:
    """Iterate the Python files of ``src/`` (and optionally ``tests/``).

    Bytecode caches are skipped — they are build artefacts, not
    sources.
    """
    roots = [SRC_ROOT]
    if include_tests:
        roots.append(TESTS_ROOT)
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def parse(path: Path) -> ast.Module:
    """Parse ``path`` as an AST module."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.Module) -> set[str]:
    """Return the set of absolute modules imported by the module.

    Relative imports (a leading dot) are skipped: they stay inside
    the owning package and cannot cross a package boundary.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                modules.add(node.module)
    return modules


def markdown_link_targets(text: str) -> list[str]:
    """Return every repository-relative Markdown link target in ``text``.

    Skipped, because they are not references into this repository:

    - external schemes (``http``, ``https``, ``mailto``, ``file``);
    - pure anchors (``#section``);
    - **placeholders** — a target containing an angle-bracket slot
      (``../<file>.md``) or the ``XXX`` marker. A template shows the
      *shape* of a link, not a link.
    """
    targets: list[str] = []
    for match in MARKDOWN_LINK.finditer(text):
        raw = match.group(1).split("#")[0].strip()
        if "<" in raw or ">" in raw or "XXX" in raw:
            continue
        target = raw.strip("<>")
        if not target or target.startswith(EXTERNAL_SCHEMES):
            continue
        targets.append(target)
    return targets


def link_resolves(source: Path, target: str) -> bool:
    """True when the relative link ``target`` resolves from ``source``.

    ``source`` is the file containing the link; the target is
    interpreted relative to the file's own directory.
    """
    return (source.parent / target).resolve().exists()


def iter_markdown_files(root: Path) -> list[Path]:
    """Return the Markdown files under ``root``, sorted."""
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.md"))


def normative_markdown_files() -> list[Path]:
    """Return the **normative** Markdown surface of the repository.

    The normative surface is what an agent reads as a source of
    truth: the agent skills, the transversal policies, and the
    repository-root documents.

    Two trees are deliberately excluded:

    - ``ADRs/`` and ``CHANGELOG.md`` are **historical records**. A
      link that pointed somewhere valid when the record was written
      is not an invariant; rewriting history would falsify the record.
    - ``docs/`` is the **reference** tree, not the normative one. It
      carries a large volume of as-found documentation whose residual
      broken links are inventoried by ``scripts/debt.py`` and
      corrected under ``migrate-on-touch``. Failing the CI on them
      would be a rule nobody could satisfy.

    The residual debt in the excluded trees is inventoried rather than
    enforced.
    """
    files: list[Path] = []
    files.extend(iter_markdown_files(SKILLS_ROOT))
    files.extend(iter_markdown_files(RULES_ROOT))
    for name in (
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "GETTING_STARTED.md",
        "REFERENCE.md",
        "ROADMAP.md",
        "SECURITY.md",
    ):
        candidate = PROJECT_ROOT / name
        if candidate.exists():
            files.append(candidate)
    return sorted(
        {
            path
            for path in files
            if "adr-history" not in path.relative_to(PROJECT_ROOT).parts
        }
    )
