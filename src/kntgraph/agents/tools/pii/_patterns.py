# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regex patterns and default label set for PII redaction.

Centralises the configurable shapes (the regex patterns and
the labels consumed by the NER extractor) so changing the
rules does not touch the tool class.
"""

from __future__ import annotations

import re
from typing import Union


# Framework-level recursive type for the payload
# accepted by the PII redaction tool. The redactor
# walks the payload recursively (dict / list /
# scalar); scalars are strings, ints, floats, bools,
# or ``None``. Defining the type here (instead of in
# ``_tool``) avoids an import cycle with ``_level1``
# and ``_level2``.
PiiScalar = Union[str, int, float, bool, None]
PiiPayload = Union[
    PiiScalar,
    dict[str, "PiiPayload"],
    list["PiiPayload"],
]


# Default label set for level 2/3. Same strings are used
# as `Entity.type` and as the `:Entity` label in FalkorDB
# queries (when entities are projected — out of scope for
# the Solution tier in the MVP).
DEFAULT_PII_LABELS: tuple[str, ...] = (
    "cpf",
    "cnpj",
    "email",
    "telefone",
    "endereco",
    "nome_pessoa",
    "chave_pix",
    "cartao_credito",
)


# Regex patterns for level 1. All patterns are case-
# insensitive and tolerate the common Brazilian
# formatting. They are deliberately conservative — false
# negatives are acceptable, false positives are not (we
# do not want to break legitimate data).
#
# Each pattern is anchored (`\b`) and well-formed. We
# apply patterns in a specific ORDER to avoid one
# pattern eating the input of another (e.g. CEP eating
# the first 8 digits of a PIX key). The order in
# `PATTERNS` reflects this: more-specific patterns
# (CNPJ, CPF, phone) come before less-specific ones
# (CEP, PIX, card).
_RE_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_RE_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Brazilian phone: optional `+55`, optional `(`, two
# digits, optional `)`, optional `9`, 4 digits, optional
# `-`, 4 digits. The pattern is permissive on spacing
# but anchored to digit boundaries so it does not eat
# a CNPJ or PIX.
_RE_PHONE_BR = re.compile(r"\+?55\s?\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b")
# CEP: exactly 5 digits, optional `-`, exactly 3 digits.
# Word boundaries prevent the regex from matching
# subsets of longer digit runs.
_RE_CEP = re.compile(r"\b\d{5}-?\d{3}\b")
# PIX key UUID-shaped: 8-4-4-4-12 hex. The pattern
# requires the dashes (we don't match unseparated 32-hex
# strings; those are likely other IDs).
_RE_PIX = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Credit-card-like strings: 4 groups of 4 digits
# separated by spaces or dashes. We do NOT match
# 13-19 contiguous digits; the contiguous form is too
# greedy (catches PIX UUIDs, CNPJs, etc.).
_RE_CARD = re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b")


# Map of regex → placeholder. The placeholder is the
# framework's convention; consumers of the redacted
# payload see e.g. `<PII:cnpj>` in place of the value.
# Order matters: more-specific patterns first so they
# have a chance to match before less-specific ones
# consume the digits.
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_RE_CNPJ, "cnpj"),
    (_RE_CPF, "cpf"),
    (_RE_EMAIL, "email"),
    (_RE_PHONE_BR, "telefone"),
    (_RE_PIX, "chave_pix"),
    (_RE_CARD, "cartao_credito"),
    (_RE_CEP, "cep"),
)
