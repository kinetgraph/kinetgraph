# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
HeuristicEntityExtractor — default, dependency-free
Entity extractor.

Recognises:

  - CNPJ (with or without formatting).
  - CPF (with or without formatting).
  - Brazilian money literals (R$ 1.234,56).
  - ISO dates (`2024-05-12`, `2024-05-12T10:00:00Z`).
  - Brazilian dates (`12/05/2024`).
  - Short alphanumeric IDs (e.g. `NF-00123`, `INVC/2024/0001`).
  - Payload-keyed entities: any of `supplier`, `customer`,
    `cnpj`, `amount`, `issue_date`, etc. The matching
    value becomes an Entity of the hinted type.

The extractor NEVER calls an LLM and NEVER hits the
network. It is the safe default for the cold path and
for tests. LLM-based extractors subclass or compose —
they are Roles, not Tools (ADR-006).

Detection strategy
------------------

Two passes:

  1. **Free-text scan** — regex over the raw text. Catches
     `R$`, CNPJs/CPFs, dates and IDs that appear in the
     rendered JSON.

  2. **Payload scan** — if the text is a JSON-encoded dict,
     walk the keys and pick values for known type hints.
     The walk is one level of nesting deep. Lists are
     NOT walked (their contents are usually the entities
     themselves, e.g. `itens=[]` — picking them yields
     high-cardinality noise).

Both passes feed a single `Entity` list; the public
`extract` dedups by canonical key. The same Entity can
match twice (e.g. `NF-12345` matches `_ID_RE` in the text
scan and also appears as `data.document_id` in the payload
scan). The first occurrence wins, which is the convention
of `dedup_entities`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ...core._typing import JsonValue
from .base import (
    Entity,
    dedup_entities,
    parse_payload,
)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Brazilian fiscal IDs and common numerics. The CNPJ/CPF
# patterns accept both formatted ("12.345.678/0001-90") and
# unformatted ("12345678000190") variants. The boundary
# `\b` keeps the regex from over-matching inside larger
# digit runs.

_CNPJ_RE = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_MONEY_RE = re.compile(r"R\$\s*\d{1,3}(?:\.\d{3})*(?:,\d{2})?")
_DATE_ISO_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}"
    r"(?:[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_DATE_BR_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
# Generic identifier-like patterns. Catches `NF-001`,
# `NF-12345`, `INVC/2024/0001`, `INVC-2024-0001`,
# `BANCO/2024/000123`. Excludes pure-digit IDs (those
# are already caught by CNPJ/CPF). The structure is:
# `prefix(2-6 uppercase) + optional separator + numeric
# segments(2+ digits each) optionally chained by - or /`.
# The greedy `\d+` is contained inside an optional group
# so a separator run is consumed only if more digits
# follow. Examples:
#   "NF-001"          → match
#   "INVC/2024"       → match (prefix "INVC", digits "2024")
#   "INVC/2024/0001"  → match (digits segments chained)
#   "IN"              → no match (digits required)
#   "OF-2024-X"       → no match (last segment must be digits)
_ID_RE = re.compile(r"\b[A-Z]{2,6}[-/]?\d{2,}(?:[-/]\d+)*\b")


# ---------------------------------------------------------------------------
# Payload-key type hints
# ---------------------------------------------------------------------------

# Map of well-known payload keys to entity types. The
# matching is case-insensitive on the key. Applications
# that need different hints subclass `HeuristicEntityExtractor`
# and override `_KEY_TYPE_HINTS`.
KEY_TYPE_HINTS: dict[str, str] = {
    # Org-like
    "supplier": "org",
    "vendor": "org",
    "customer": "org",
    "issuer": "org",
    "recipient": "org",
    "legal_name": "org",
    # ID-like
    "cnpj": "id",
    "cpf": "id",
    "document_id": "id",
    "invoice_id": "id",
    "access_key": "id",
    "order_id": "id",
    "transaction_id": "id",
    # Money-like
    "amount": "money",
    "total": "money",
    "price": "money",
    # Date-like
    "issue_date": "date",
    "date": "date",
    "due_date": "date",
    # Location-like
    "city": "location",
    "state": "location",
    "country": "location",
}


# ---------------------------------------------------------------------------
# HeuristicEntityExtractor
# ---------------------------------------------------------------------------


class HeuristicEntityExtractor:
    """
    Deterministic, dependency-free Entity extractor.

    No I/O. Same input → same output. Safe default for the
    cold path and for tests; LLM-based extractors subclass
    or compose (they are Roles, not Tools — ADR-006).
    """

    # Public so subclasses can extend without redefining
    # the format. Use the class attribute (not instance
    # state) so the lookup is O(1) without allocation.
    _KEY_TYPE_HINTS: dict[str, str] = KEY_TYPE_HINTS

    # ------------------------------------------------------------------ text

    def _scan_text(self, text: str) -> list[Entity]:
        """
        Free-text scan: regex passes.

        The order of operations does not affect uniqueness;
        the deduplication happens at the call site (and at
        the projector, via `MERGE`). Each match is added
        as an Entity with `surface` equal to the matched
        text.
        """
        out: list[Entity] = []
        for m in _CNPJ_RE.finditer(text):
            out.append(_mk(m.group(0), "id"))
        for m in _CPF_RE.finditer(text):
            out.append(_mk(m.group(0), "id"))
        for m in _MONEY_RE.finditer(text):
            out.append(_mk(m.group(0), "money"))
        for m in _DATE_ISO_RE.finditer(text):
            out.append(_mk(m.group(0), "date"))
        for m in _DATE_BR_RE.finditer(text):
            out.append(_mk(m.group(0), "date"))
        for m in _ID_RE.finditer(text):
            out.append(_mk(m.group(0), "id"))
        return out

    # ------------------------------------------------------------------ payload

    def _scan_payload(self, payload: Mapping[str, JsonValue]) -> list[Entity]:
        """
        Structured scan: walk the payload dict, pick values
        for keys with known type hints.

        Nested dicts are walked one level deep (e.g.
        `payload.customer.cnpj` yields a CNPJ entity under
        the `org` type hint of the parent). Lists are NOT
        walked — the list contents are usually the entities
        themselves, and picking them produces
        high-cardinality noise (e.g. one entity per `entries`
        item).

        Unknown keys are skipped. Empty string / None values
        are skipped.
        """
        out: list[Entity] = []
        for k, v in payload.items():
            t = self._KEY_TYPE_HINTS.get(k.lower())
            if t is None:
                continue
            if isinstance(v, (str, int, float)):
                surface = str(v)
                if not surface:
                    continue
                out.append(_mk(surface, t))
            elif isinstance(v, dict):
                # One level of nesting.
                for k2, v2 in v.items():
                    t2 = self._KEY_TYPE_HINTS.get(k2.lower(), t)
                    if isinstance(v2, (str, int, float)) and v2 != "":
                        out.append(_mk(str(v2), t2))
        return out

    # ------------------------------------------------------------------ public

    async def extract(self, text: str) -> list[Entity]:
        """
        Extract entities from `text`.

        If `text` is a JSON payload (detected by a leading
        `{` and a parseable body), the extractor ALSO
        scans the structured payload via `_scan_payload`.
        Free-text documents are still scanned via
        `_scan_text`.

        Order is not significant; the result is deduped by
        canonical `(name, type)` key.
        """
        if not text:
            return []
        entities: list[Entity] = self._scan_text(text)
        payload = parse_payload(text)
        if payload is not None:
            entities.extend(self._scan_payload(payload))
        return dedup_entities(entities)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mk(surface: str, etype: str) -> Entity:
    """
    Build an `Entity` from a matched surface string.

    `surface` is what the regex / payload saw; `name` is
    computed by `Entity.__post_init__` via `canonicalize`.
    """
    return Entity(name=surface, type=etype, surface=surface)


__all__ = [
    "HeuristicEntityExtractor",
    "KEY_TYPE_HINTS",
]
