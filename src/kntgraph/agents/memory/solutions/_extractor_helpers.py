# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Internal helpers for :class:`SolutionExtractor`.

The tag-conversion logic and the CNPJ-shape heuristic live
here; they are not part of the public API and are only
invoked from :mod:`_extractor`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.knowledge.extraction.base import Entity as _EntityT


# ``_EntityLike`` is the framework-level name for the
# duck-typed shape this module reads: any object that
# exposes ``.type: str`` and ``.name: str`` works.
# We type it as the concrete :class:`Entity` (the
# production value); tests can pass mocks and the
# runtime check is satisfied via attribute access.
def _entities_to_tags(
    entities: Iterable["_EntityT"],
) -> dict[str, str]:
    """
    Convert a list of `Entity` to the framework's
    default tag dict. The mapping is heuristic and
    application-overridable (see
    `SolutionExtractor._extract_tags`).

    Tags are stored as `str → str` for JSON-friendly
    serialisation. Numeric values are stringified
    verbatim. Non-string surfaces are dropped (the
    `name` is always a string after `canonicalize`).
    """
    tags: dict[str, str] = {}
    for e in entities:
        if e.type == "id" and _looks_like_cnpj(e.name):
            tags.setdefault("cnpj", e.name)
        elif e.type == "org":
            tags.setdefault("supplier", e.name)
        elif e.type == "location" and len(e.name) == 2:
            tags.setdefault("uf", e.name.upper())
        elif e.type == "date":
            tags.setdefault("date", e.name)
        elif e.type == "money":
            tags.setdefault("amount", e.name)
    return tags


_CNPJ_DIGITS_RE = re.compile(r"^[\d./\-]+$")
_CNPJ_DIGIT_COUNT_RE = re.compile(r"\d")


def _looks_like_cnpj(name: str) -> bool:
    """
    True for canonicalised CNPJ-like strings.

    A canonicalised CNPJ is either 14 pure digits (the
    `canonicalize` in `Entity` does NOT strip
    punctuation, so "12.345.678/0001-90" stays
    punctuated) or a punctuated form. The heuristic
    here accepts any string whose digit count is 14 and
    whose non-digit characters are limited to the
    punctuation that appears in formatted CNPJs
    (`.`, `/`, `-`). This is enough to disambiguate
    from dates ("2024-05-12" has 8 digits) and IDs
    ("NF-00123" has 5).
    """
    digit_count = len(_CNPJ_DIGIT_COUNT_RE.findall(name))
    if digit_count != 14:
        return False
    return bool(_CNPJ_DIGITS_RE.match(name))
