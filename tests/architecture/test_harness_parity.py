# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Architecture check — parity between the harness skill and the checks.

The harness guide's golden rule (Nível 4):

> A new rule is always born in two places: a check
> (``tests/architecture/``) **and** the rule in the skill. A rule
> without a check is prose — and prose ages.

This check enforces that rule in **both** directions:

1. **Every check on disk is registered in the skill.** A check that
   the skill does not mention is a rule nobody knows about: the next
   agent will not know why the CI is red, and will invent an
   exception.
2. **Every check registered in the skill exists on disk.** A skill
   entry pointing at a deleted check is documentation drift.

The contract is the *Check inventory* table of
``.agents/skills/kntgraph-harness/SKILL.md``: one row per check,
with the rule it asserts and its regime.
"""

from __future__ import annotations

import re

from tests.architecture._collectors import (
    PROJECT_ROOT,
    SKILLS_ROOT,
    relative,
)

HARNESS_SKILL = SKILLS_ROOT / "kntgraph-harness" / "SKILL.md"
ARCHITECTURE_DIR = PROJECT_ROOT / "tests" / "architecture"

#: A row of the *Check inventory* table, e.g.
#: ``| `tests/architecture/test_x.py` | The rule. | fail-fast |``.
_INVENTORY_ROW = re.compile(
    r"^\|.*`((?:tests/architecture|scripts)/[\w./-]+\.py)`.*\|",
    re.MULTILINE,
)

#: The design gate — the other enforcement mechanism of the harness.
DESIGN_GATE = "scripts/design_gate.py"


def _check_files() -> list[str]:
    """The enforcement files of the harness, repository-relative."""
    if not ARCHITECTURE_DIR.is_dir():
        return []
    files = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in ARCHITECTURE_DIR.glob("test_*.py")
    ]
    if (PROJECT_ROOT / DESIGN_GATE).exists():
        files.append(DESIGN_GATE)
    return sorted(files)


def _registered_checks() -> dict[str, str]:
    """The check paths registered in the harness skill, mapped to their row.

    Only rows of the *Check inventory* table count: a path mentioned
    in prose elsewhere in the skill is not a registration.
    """
    if not HARNESS_SKILL.exists():
        return {}
    text = HARNESS_SKILL.read_text(encoding="utf-8")
    registered: dict[str, str] = {}
    for line in text.splitlines():
        match = _INVENTORY_ROW.match(line)
        if match:
            registered[match.group(1)] = line
    return registered


def test_harness_skill_exists() -> None:
    """The harness skill exists — it is the normative source of the rules."""
    assert HARNESS_SKILL.exists(), (
        f"Missing harness skill: {relative(HARNESS_SKILL)}\n"
        "The architecture checks assert rules that must live somewhere. "
        "That somewhere is the harness skill."
    )


def test_every_check_is_registered_in_the_skill() -> None:
    """Every check file on disk appears in the skill's Check inventory."""
    registered = _registered_checks()
    offenders = [path for path in _check_files() if path not in registered]

    assert not offenders, (
        "Architecture checks that the harness skill does not register:\n"
        + "\n".join(f"  - {path}" for path in offenders)
        + "\n\nA rule without a check ages; a check without a rule is "
        "mystery. See the Check inventory table in "
        ".agents/skills/kntgraph-harness/SKILL.md."
    )


def test_every_registered_check_exists() -> None:
    """Every check registered in the skill exists on disk."""
    offenders = sorted(
        path for path in _registered_checks() if not (PROJECT_ROOT / path).exists()
    )

    assert not offenders, (
        "Checks registered in the harness skill that do not exist:\n"
        + "\n".join(f"  - {path}" for path in offenders)
        + "\n\nThe skill describes the rules; the checks enforce them. "
        "They must not drift apart."
    )


def test_skill_declares_a_regime_for_every_check() -> None:
    """Every registered check declares its regime (fail-fast or conditional).

    The regime decides whether a violation breaks the CI immediately
    or is merely inventoried. A check without a declared regime is a
    check whose failure behaviour is a matter of opinion.
    """
    offenders = [
        path
        for path, row in _registered_checks().items()
        if "fail-fast" not in row and "conditional" not in row
    ]

    assert not offenders, (
        "Checks whose regime is not declared in the skill:\n"
        + "\n".join(f"  - {path}" for path in offenders)
        + "\n\nDeclare each check as fail-fast or conditional."
    )
