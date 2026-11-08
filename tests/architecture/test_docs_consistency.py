# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Architecture check — referential integrity of the normative surface.

Invariants asserted here:

1. **Every relative Markdown link on the normative surface resolves.**
   The normative surface is what an agent reads as a source of truth:
   the agent skills, the transversal policies, the documentation tree
   and the repository-root documents. A broken link there means the
   agent receives a 404 in the middle of its reasoning — worse than
   no link at all.

2. **Every ADR file name is well-formed** (``ADR-NNN-Title.md``).

3. **``AGENTS.md`` and the skills directory agree.** Every skill on
   disk is listed in ``AGENTS.md``, and every skill named in
   ``AGENTS.md`` exists on disk.

> **Scope.** Historical records — the ADRs themselves and
> ``CHANGELOG.md`` — are excluded. A link that was valid when a
> record was written is not an invariant, and rewriting history would
> falsify the record. The residual debt in those trees is
> *inventoried* by ``scripts/debt.py`` rather than enforced here.
"""

from __future__ import annotations

import re

import pytest

from tests.architecture._collectors import (
    PROJECT_ROOT,
    SKILLS_ROOT,
    link_resolves,
    markdown_link_targets,
    normative_markdown_files,
    relative,
)

AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"
ADRS_DIR = PROJECT_ROOT / "ADRs"

#: The mandatory ADR file-name shape.
_ADR_NAME = re.compile(r"^ADR-\d{3}-[A-Za-z0-9._-]+\.md$")

#: A skill name mentioned in ``AGENTS.md``.
_SKILL_MENTION = re.compile(r"`(kntgraph-[a-z0-9-]+)`")


def test_normative_links_resolve() -> None:
    """Every relative Markdown link on the normative surface resolves."""
    offenders: list[str] = []

    for path in normative_markdown_files():
        text = path.read_text(encoding="utf-8")
        for target in markdown_link_targets(text):
            if link_resolves(path, target):
                continue
            offenders.append(f"{relative(path)} -> {target}")

    assert not offenders, (
        "Broken relative links on the normative surface:\n"
        + "\n".join(f"  - {offender}" for offender in offenders)
        + "\n\nA broken link makes the agent read a 404 mid-reasoning. "
        "Fix the link or remove it."
    )


def test_adr_file_names_are_well_formed() -> None:
    """Every ADR file name matches ``ADR-NNN-Title.md``."""
    if not ADRS_DIR.is_dir():
        pytest.skip("ADRs directory is absent")

    offenders = [
        path.name
        for path in sorted(ADRS_DIR.glob("*.md"))
        if not _ADR_NAME.match(path.name)
    ]

    assert not offenders, (
        "Malformed ADR file names (expected ADR-NNN-Title.md):\n"
        + "\n".join(f"  - {name}" for name in offenders)
    )


def test_every_skill_is_listed_in_agents_md() -> None:
    """Every skill directory has a row in the ``AGENTS.md`` index table.

    The table — not a passing mention in prose — is the index an
    agent reads to discover which skills exist. A skill that is only
    named in a paragraph is a skill that will not be found.
    """
    if not AGENTS_FILE.exists():
        pytest.skip("AGENTS.md is absent")

    text = AGENTS_FILE.read_text(encoding="utf-8")
    index_rows = [line for line in text.splitlines() if line.lstrip().startswith("|")]
    offenders = [
        path.name
        for path in sorted(SKILLS_ROOT.iterdir())
        if path.is_dir() and not any(f"`{path.name}`" in row for row in index_rows)
    ]

    assert not offenders, (
        "Skills on disk that the AGENTS.md index table does not list:\n"
        + "\n".join(f"  - {name}" for name in offenders)
        + "\n\nAGENTS.md is the entry point an agent reads first. An "
        "unlisted skill is a skill that will never be loaded."
    )


def test_every_skill_named_in_agents_md_exists() -> None:
    """Every skill named in ``AGENTS.md`` exists on disk."""
    if not AGENTS_FILE.exists():
        pytest.skip("AGENTS.md is absent")

    text = AGENTS_FILE.read_text(encoding="utf-8")
    offenders = [
        name
        for name in _SKILL_MENTION.findall(text)
        if not (SKILLS_ROOT / name).is_dir()
    ]

    assert not offenders, (
        "Skills named in AGENTS.md that do not exist on disk:\n"
        + "\n".join(f"  - {name}" for name in offenders)
    )
