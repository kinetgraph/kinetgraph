# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Architecture check — validator for ``docs/tasks/*.md`` (Definition of Done).

Invariants asserted here (source: ``docs/tasks/README.md``):

1. **Every ``task.md`` has the mandatory structure** — the ``BC``,
   ``Feature doc``, ``Status`` and ``Criado`` fields.
2. **Every Definition-of-Done item carries at least one ``check:``** —
   an item without a check is not verifiable, and therefore is not
   part of a Definition of Done.
3. **Every ``check:`` points at something that exists** — the file it
   names, and the test node inside it, when a node is named.
4. **``Status: DONE`` requires every item to be ticked** — a task
   cannot declare itself finished with a pending Definition of Done.
5. **``Status`` is one of the known values.**

> **Scope.** This validator asserts that the Definition of Done is
> **verifiable** (the checks exist and address a real target). It does
> **not** run them — the CI gate does. The separation is deliberate:
> structural validation is fast and deterministic; execution is
> expensive.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.architecture._collectors import PROJECT_ROOT, relative

TASKS_DIR = PROJECT_ROOT / "docs" / "tasks"

#: The statuses a task may declare.
VALID_STATUSES = frozenset({"PLANNING", "IN_PROGRESS", "BLOCKED", "DONE"})

#: A Definition-of-Done item: ``- [ ] **D1** — description``.
_DOD_ITEM = re.compile(r"^(\s*)- \[( |x)\] \*\*([^*]+)\*\*\s*(.*)$")

#: A check line: ``- check: `command``` (indentation optional).
_CHECK_LINE = re.compile(r"^[ \t]*-[ \t]*check:[ \t]*`(.+?)`[ \t]*$", re.MULTILINE)

#: Commands a check may invoke.
_CHECK_COMMAND = re.compile(
    r"(?:pytest|ruff|pyright|radon|bandit|ci\.py|debt\.py|python)\b"
)

#: A reference to a test node: ``path::test_name`` or ``path::Class::test``.
_TEST_NODE = re.compile(r"([\w./\-]+\.py)(?:::([\w:]+))?")

#: A Markdown link, e.g. ``[label](target)``.
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _task_files() -> list[Path]:
    """The task plans, excluding the guide and the template."""
    if not TASKS_DIR.exists():
        return []
    return [
        path
        for path in sorted(TASKS_DIR.glob("*.md"))
        if path.name not in {"README.md", "TEMPLATE.md"}
    ]


def _parse_task(path: Path) -> tuple[list[tuple[bool, str, str]], list[str]]:
    """Extract the Definition-of-Done items and the orphan checks.

    Returns:
        The items as ``(ticked, id, description)`` triples, plus the
        ``check:`` lines that belong to no item.
    """
    items: list[tuple[bool, str, str]] = []
    orphan_checks: list[str] = []
    current: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        item_match = _DOD_ITEM.match(raw_line)
        if item_match:
            ticked = item_match.group(2) == "x"
            item_id = item_match.group(3).strip()
            description = item_match.group(4).strip()
            items.append((ticked, item_id, description))
            current = item_id
            continue

        if _CHECK_LINE.match(raw_line):
            if current is None:
                orphan_checks.append(
                    f"{relative(path)}: check without item: {raw_line.strip()}"
                )
            continue

        # An unindented, non-check line ends the current item.
        if raw_line.strip() and not raw_line.startswith((" ", "\t")):
            current = None

    return items, orphan_checks


def _collect_check_commands(path: Path) -> list[str]:
    """Every ``check:`` command declared in the file."""
    commands: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = _CHECK_LINE.match(raw_line)
        if match:
            commands.append(match.group(1))
    return commands


def _test_node_exists(path: str, node: str | None) -> bool:
    """True when the file — and optionally the test node — exists."""
    file_path = PROJECT_ROOT / path
    if not file_path.exists():
        return False
    if not node:
        return True
    content = file_path.read_text(encoding="utf-8")
    for part in (piece for piece in node.split("::") if piece):
        pattern = (
            rf"^\s*(async\s+)?def\s+{re.escape(part)}\b|^class\s+{re.escape(part)}\b"
        )
        if not re.search(pattern, content, re.MULTILINE):
            return False
    return True


def test_tasks_directory_exists() -> None:
    """The ``docs/tasks/`` directory exists, with its guide and template."""
    assert TASKS_DIR.exists(), f"Task directory missing: {TASKS_DIR}"
    assert (TASKS_DIR / "README.md").exists(), (
        "docs/tasks/README.md missing — it documents the verifiable "
        "Definition-of-Done format"
    )
    assert (TASKS_DIR / "TEMPLATE.md").exists(), (
        "docs/tasks/TEMPLATE.md missing — it is the starting point for "
        "every execution plan"
    )


@pytest.mark.parametrize("task_path", _task_files(), ids=lambda p: p.name)
def test_task_has_required_structure(task_path: Path) -> None:
    """Every task plan declares BC, Feature doc, Status and Criado."""
    text = task_path.read_text(encoding="utf-8")
    missing = [
        field
        for field in ("**BC:**", "**Feature doc:**", "**Status:**", "**Criado:**")
        if field not in text
    ]

    assert not missing, (
        f"{relative(task_path)}: mandatory fields missing: {missing}\n"
        "See the format in docs/tasks/README.md"
    )


@pytest.mark.parametrize("task_path", _task_files(), ids=lambda p: p.name)
def test_task_status_is_valid(task_path: Path) -> None:
    """The task's ``Status`` is one of the known values."""
    text = task_path.read_text(encoding="utf-8")
    match = re.search(r"\*\*Status:\*\*\s*`?([A-Z_]+)`?", text)
    assert match, f"{relative(task_path)}: Status field not found"

    status = match.group(1)
    assert status in VALID_STATUSES, (
        f"{relative(task_path)}: invalid Status {status!r} "
        f"(expected one of: {sorted(VALID_STATUSES)})"
    )


@pytest.mark.parametrize("task_path", _task_files(), ids=lambda p: p.name)
def test_feature_doc_link_resolves(task_path: Path) -> None:
    """The ``Feature doc`` field points at a document that exists.

    The field may hold a plain path or a Markdown link. When it holds
    a link, the target must resolve — a plan pointing at a document
    that does not exist is a plan nobody can act on.
    """
    text = task_path.read_text(encoding="utf-8")
    match = re.search(r"\*\*Feature doc:\*\*\s*(.+)", text)
    assert match, f"{relative(task_path)}: Feature doc field not found"

    field = match.group(1).strip()
    link = _MARKDOWN_LINK.search(field)
    if link is None:
        # A plain path. Accept it only when it is not a placeholder.
        target = field.strip("`").strip()
        if "<" in target or not target:
            pytest.skip(f"{relative(task_path)}: Feature doc is a placeholder")
        resolved = (task_path.parent / target).resolve()
        assert resolved.exists(), (
            f"{relative(task_path)}: Feature doc does not exist: {target}"
        )
        return

    target = link.group(1).split("#")[0].strip()
    resolved = (task_path.parent / target).resolve()
    assert resolved.exists(), (
        f"{relative(task_path)}: Feature doc link does not resolve: {target}"
    )


@pytest.mark.parametrize("task_path", _task_files(), ids=lambda p: p.name)
def test_every_dod_item_has_a_check(task_path: Path) -> None:
    """Every Definition-of-Done item carries at least one ``check:``."""
    text = task_path.read_text(encoding="utf-8")
    items, _ = _parse_task(task_path)
    if not items:
        pytest.skip(f"{relative(task_path)}: no Definition-of-Done items yet")

    offenders: list[str] = []
    for ticked, item_id, _ in items:
        pattern = re.compile(
            rf"- \[{'x' if ticked else ' '}\] \*\*{re.escape(item_id)}\*\*"
            rf"(.*?)(?=\n[ \t]*-[ \t]*\[|\Z)",
            re.DOTALL,
        )
        block = pattern.search(text)
        if not block:
            offenders.append(f"{item_id} (item not found)")
            continue
        if not _CHECK_LINE.search(block.group(1)):
            offenders.append(item_id)

    assert not offenders, (
        f"{relative(task_path)}: Definition-of-Done items without a "
        f"`check:` — an item without a check is not verifiable: {offenders}\n"
        "See docs/tasks/README.md"
    )


@pytest.mark.parametrize("task_path", _task_files(), ids=lambda p: p.name)
def test_dod_checks_point_to_existing_targets(task_path: Path) -> None:
    """Every ``check:`` names a command whose target exists."""
    offenders: list[str] = []
    for command in _collect_check_commands(task_path):
        if not _CHECK_COMMAND.search(command):
            offenders.append(f"unrecognised command: {command!r}")
            continue
        for path, node in _TEST_NODE.findall(command):
            if not _test_node_exists(path, node):
                offenders.append(
                    f"target does not exist: {path}" + (f"::{node}" if node else "")
                )

    assert not offenders, (
        f"{relative(task_path)}: checks pointing at targets that do not exist:\n"
        + "\n".join(f"  - {offender}" for offender in offenders)
    )


@pytest.mark.parametrize("task_path", _task_files(), ids=lambda p: p.name)
def test_no_orphan_checks(task_path: Path) -> None:
    """No ``check:`` line sits outside a Definition-of-Done item."""
    _, orphans = _parse_task(task_path)
    assert not orphans, f"{relative(task_path)}: orphan checks:\n" + "\n".join(
        f"  - {orphan}" for orphan in orphans
    )


@pytest.mark.parametrize("task_path", _task_files(), ids=lambda p: p.name)
def test_done_status_requires_all_items_checked(task_path: Path) -> None:
    """``Status: DONE`` requires every Definition-of-Done item to be ticked."""
    text = task_path.read_text(encoding="utf-8")
    status_match = re.search(r"\*\*Status:\*\*\s*`?([A-Z_]+)`?", text)
    if not status_match or status_match.group(1) != "DONE":
        pytest.skip(f"{relative(task_path)}: status is not DONE")

    items, _ = _parse_task(task_path)
    pending = [item_id for ticked, item_id, _ in items if not ticked]

    assert not pending, (
        f"{relative(task_path)}: Status=DONE but the Definition of Done "
        f"still has pending items: {pending}"
    )
