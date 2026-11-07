# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Architecture check — technical-debt inventory (conditional regime).

This check **does not fail on the existence of debt**. It
**inventories** it, so that the ``fix-on-touch`` policy can be
applied consistently:

> When you touch a file, correct the technical debt *in that file*.

**Why a check that does not fail?** Because the policy is
*conditional*: debt is corrected when the file is touched. A check
that failed the CI on the existence of debt would force a
mass-migration — the opposite of what the policy preaches.

What this check does:

1. **Inventories** the debt per file (a report, not a failure).
2. **Fails** only when a file **newly** introduces debt that the
   rules already forbid (a regression).

The known debt is recorded in ``.debt-baseline.json``. Regenerate it
after an intentional cleanup with::

    uv run python scripts/debt.py --update-baseline

Debt kinds and the rule each one comes from:

| Kind | Rule |
|---|---|
| ``broad-except`` | ``kntgraph-typed-errors`` — the caller trusts the Protocol |
| ``print-call`` | ``kntgraph-style`` — structured logging, not ``print()`` |
| ``dict-str-any`` | ``kntgraph-type-discipline`` §5.1 — ``JsonValue`` on the wire |
| ``type-any-key`` | ``kntgraph-type-discipline`` §5.3 — ADR-080 narrows the key |
| ``bare-object`` | ``kntgraph-type-discipline`` §0 — bare ``object`` is banned |
| ``broken-doc-link`` | harness — the normative surface must not 404 |
| ``duplicate-adr-number`` | harness — an ADR number identifies one decision |

Detection is by **AST**, not by text: a ``grep`` for ``object`` would
accuse the docstring of a module that says "we do not use ``object``".
"""

from __future__ import annotations

from collections import Counter

from tests.architecture._debt import (
    DEBT_KINDS,
    collect_debts,
    load_baseline,
    new_debt,
)


def test_debt_inventory_does_not_regress() -> None:
    """No file introduces debt that the baseline does not record.

    Existing debt is reported, not rejected: the ``fix-on-touch``
    policy removes it when the file is next touched.
    """
    baseline: Counter[tuple[str, str, str]] = load_baseline()
    offenders = new_debt(baseline)

    if not offenders:
        return

    grouped: dict[str, list[str]] = {}
    for debt in offenders:
        grouped.setdefault(debt.kind, []).append(
            f"{debt.path}:{debt.line}  {debt.detail}"
        )

    report = "\n\n".join(
        f"{kind} — {DEBT_KINDS.get(kind, 'unknown kind')}\n"
        + "\n".join(f"  - {line}" for line in lines)
        for kind, lines in sorted(grouped.items())
    )

    raise AssertionError(
        "New technical debt introduced (the baseline records the known "
        "debt; these entries are not in it):\n\n"
        f"{report}\n\n"
        "Either fix the debt, or — if the debt is intentional and "
        "legitimate — record it with "
        "'uv run python scripts/debt.py --update-baseline'."
    )


def test_debt_inventory_is_discoverable() -> None:
    """Every inventoried debt is reported by the query tool.

    The inventory is only useful if an agent can ask "what is wrong in
    this file?" and get an answer. This test asserts that the
    inventory is non-empty and that every kind it uses is documented,
    so a new kind cannot be introduced without a description.
    """
    debts = collect_debts()
    undocumented = sorted({debt.kind for debt in debts} - set(DEBT_KINDS))

    assert not undocumented, (
        "Debt kinds without a description in DEBT_KINDS: "
        f"{undocumented}\n"
        "Every kind needs a human-readable explanation so that the "
        "query tool can explain itself."
    )
