# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``FieldFinder`` Protocol and the regex-based implementation.

The Protocol is intentionally tiny -- implementations
return ``None`` when no value is found, or a
``(value, confidence)`` tuple on success. Confidence is
in ``[0, 1]`` and is compared against the extractor's
``field_threshold`` by the orchestrator.

``RegexFieldFinder`` is the pure-logic fallback (no ML
model). Useful for known formats (CNPJ, CPF, date,
money) and for tests where you want deterministic
behaviour without loading GLiNER2.

Iter 28: moved from
``kntgraph.agents.knowledge.argument_extractor._finder`` to
the framework. The vertical path is a re-export shim.
The Protocol is framework-level because (a) it is the
canonical shape for the field-find layer (consumed by
the framework's ``SchemaArgumentExtractor``) and (b) the
regex-based default is pure logic with zero
third-party deps.
"""

from __future__ import annotations

from typing import Optional, Protocol, Union, runtime_checkable

from kntgraph.tools.schema import FieldSpec


# The value side of the ``(value, confidence)`` tuple.
# JSON-typed scalars match the schema types (``string`` /
# ``integer`` / ``number``).
FieldValue = Union[str, int, float, None]


@runtime_checkable
class FieldFinder(Protocol):
    """
    Find a value for ONE field from the user's text.

    Implementations return `None` when no value is found
    for the field (NOT an error -- the caller drops the
    field). Returning a `(value, confidence)` tuple is
    the success case. Confidence is in [0, 1] and is
    compared against the extractor's `field_threshold`
    by the orchestrator.
    """

    async def find(
        self,
        text: str,
        field: FieldSpec,
    ) -> Optional[tuple[FieldValue, float]]: ...


class RegexFieldFinder(FieldFinder):
    """
    Regex-based fallback. Useful for known formats
    (CNPJ, CPF, date, money) and for tests.

    Maps a JSON-Schema `format` (or a derived pattern
    from the field name) to a regex. The first match
    wins. Confidence is always 1.0 for matches (regex
    either matches or it does not -- there is no soft
    confidence here).
    """

    _PATTERNS: dict[str, str] = {
        # CNPJ: 14 digits in `XX.XXX.XXX/XXXX-XX` form
        # (note: 4 digits for the branch, not 3) or
        # as a contiguous 14-digit run. Two alternatives
        # avoid the backtracking mess of `\.?` / `/?`
        # which made the engine skip the slash in older
        # versions of the framework.
        "cnpj": (
            r"(?:\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})"
            r"|(?:\d{14})"
        ),
        "cpf": (
            r"(?:\d{3}\.\d{3}\.\d{3}-\d{2})"
            r"|(?:\d{11})"
        ),
        "date": r"\d{4}-\d{2}-\d{2}",
        "date-time": r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}",
        "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
        "money": r"R?\$?\s*\d+[\.,]?\d{0,2}",
    }

    async def find(
        self,
        text: str,
        field: FieldSpec,
    ) -> Optional[tuple[FieldValue, float]]:
        import re

        if not text:
            return None
        # Try explicit format first, then a name hint.
        candidates: list[str] = []
        if field.format and field.format in self._PATTERNS:
            candidates.append(self._PATTERNS[field.format])
        name_lower = field.name.lower()
        for tag, pat in self._PATTERNS.items():
            if tag in name_lower and pat not in candidates:
                candidates.append(pat)
        for pat in candidates:
            m = re.search(pat, text)
            if m:
                return (m.group(0), 1.0)
        return None


__all__ = ["FieldFinder", "FieldValue", "RegexFieldFinder"]
