# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Level 1 (regex) PII redaction.

Pure logic — no async, no extractor dependency. Walks the
payload recursively and replaces PII-shaped substrings
with ``<PII:kind>`` placeholders.
"""

from __future__ import annotations

from kntgraph.agents.tools.pii._constants import PII_PLACEHOLDER_FMT
from kntgraph.agents.tools.pii._patterns import PATTERNS, PiiPayload


def redact_string(text: str) -> tuple[str, dict[str, int]]:
    """
    Apply level-1 regex redaction to a string. Returns
    the redacted text and a `kind → count` map of the
    redactions that fired.

    The text is scanned once per pattern; the same
    position cannot be matched twice because the
    redacted text does not match the regexes (the
    placeholders use `<PII:...>` which is not
    digit-heavy).
    """
    counts: dict[str, int] = {}
    out = text
    for pat, kind in PATTERNS:
        out, n = pat.subn(PII_PLACEHOLDER_FMT.format(kind=kind), out)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return out, counts


def redact_value(v: PiiPayload, counts: dict[str, int]) -> PiiPayload:
    """
    Recursively redact a payload value. Strings are
    regex-redacted; dicts are walked; lists are walked
    (their items may be PII — `itens=[{...}]` is a
    common shape); other scalars pass through.
    """
    if isinstance(v, str):
        redacted, n = redact_string(v)
        for k, c in n.items():
            counts[k] = counts.get(k, 0) + c
        return redacted
    if isinstance(v, dict):
        return {k: redact_value(x, counts) for k, x in v.items()}
    if isinstance(v, list):
        return [redact_value(x, counts) for x in v]
    return v
