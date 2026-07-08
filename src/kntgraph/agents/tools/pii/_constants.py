# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Constants shared by the PII redaction tool.

The format string for the placeholder is used in two
places (regex sub in level-1 redaction, span
substitution in level-2 NER redaction). Centralising
it here keeps the two paths in sync if the format
ever changes.
"""

from __future__ import annotations

# Placeholder format. ``{kind}`` is the PII kind
# (``cpf``, ``cnpj``, ``org``, ``email``, ...). The
# downstream consumer (LLM prompts, audit logs, etc.)
# recognises this exact shape.
PII_PLACEHOLDER_FMT: str = "<PII:{kind}>"


__all__ = ["PII_PLACEHOLDER_FMT"]
