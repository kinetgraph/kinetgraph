# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Level 2 (NER) PII redaction.

Async path that uses the ``EntityExtractor`` to find
semantic PII the regexes miss (names, addresses, etc.).
Also hosts the tree walker helpers used to flatten /
rehydrate the payload around the redaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kntgraph.agents.tools.pii._constants import PII_PLACEHOLDER_FMT
from kntgraph.agents.tools.pii._patterns import PiiPayload

if TYPE_CHECKING:
    from kntgraph.knowledge.extraction.base import EntityExtractor


# A ``Path`` is a sequence of dict keys / list indices
# that locates a position inside a ``PiiPayload`` tree.
# Each segment is either a string (dict key) or an int
# (list index).
Path = list[str | int]


def collect_strings(
    value: PiiPayload,
    path: Path,
    out: list[tuple[Path, str]],
) -> None:
    """
    Walk `value` and collect `(path, string)` pairs.

    The `path` is a list of keys/indices that locates
    the string in the original structure. `out` is
    mutated.
    """
    if isinstance(value, str):
        out.append((list(path), value))
        return
    if isinstance(value, dict):
        for k, v in value.items():
            collect_strings(v, path + [str(k)], out)
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            collect_strings(v, path + [i], out)


def set_at_path(root: PiiPayload, path: Path, value: PiiPayload) -> None:
    """
    In-place replacement of the value at `path` inside
    `root`. Walks the path segment by segment; the
    last segment is where the value is set. Raises
    `KeyError` / `IndexError` if the path is invalid
    (caller passes paths it collected; mismatch is a
    bug).
    """
    if not path:
        # Caller passed the root itself; we cannot
        # replace `root` in place. The redaction
        # contract expects the root to be a dict or
        # list — never a bare string — so this branch
        # is unreachable in practice.
        return
    node = root
    for key in path[:-1]:
        node = node[key]  # type: ignore[index]
    node[path[-1]] = value  # type: ignore[index]


async def ner_redact(
    payload: PiiPayload,
    counts: dict[str, int],
    *,
    entity_extractor: "EntityExtractor",
    labels: tuple[str, ...],
) -> None:
    """
    Walk a redacted payload, run the entity extractor on
    each string value, and replace entity names with
    ``<PII:{type}>`` placeholders.

    The walker is recursive (the structure of the
    payload is preserved). Strings are the only values
    inspected; the placeholders from level 1 are
    themselves not PII (they are ``<...>`` markers) and
    the extractor is unlikely to match them.
    """
    # Flatten the payload into a list of
    # (path, string) pairs; redact each string in
    # place; reassemble.
    # The simplest implementation walks the tree
    # twice (collect, then replace); for typical
    # tool payloads (a few dozen keys) the cost
    # is negligible. A streaming implementation
    # could be added in Fase 4.
    paths: list[tuple[Path, str]] = []
    collect_strings(payload, [], paths)
    for path, original in paths:
        entities = await entity_extractor.extract(original)
        if not entities:
            continue
        redacted = original
        for ent in entities:
            if ent.type not in labels:
                continue
            # Replace the canonical name (case-folded,
            # possibly punctuated) with the placeholder.
            placeholder = PII_PLACEHOLDER_FMT.format(kind=ent.type)
            # Use the original surface form for the
            # find (the canonical name may be
            # different from the surface). But the
            # entity surface is what the extractor
            # saw, which is the `original` text.
            if ent.surface and ent.surface in redacted:
                redacted = redacted.replace(ent.surface, placeholder, 1)
                counts[ent.type] = counts.get(ent.type, 0) + 1
            elif ent.name and ent.name in redacted:
                redacted = redacted.replace(ent.name, placeholder, 1)
                counts[ent.type] = counts.get(ent.type, 0) + 1
        set_at_path(payload, path, redacted)
