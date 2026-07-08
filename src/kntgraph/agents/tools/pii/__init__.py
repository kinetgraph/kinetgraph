# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
PiiRedactionTool — the PII gate for the Solution tier
(ADR-010 §2.5).

The tool implements the ``Tool`` Protocol (lives in core,
not in ``kntgraph.agents`` — PII is transversal) and exposes a
single capability: ``redact(payload)``. The caller
(``SolutionPromoter``, Fase 3) invokes the tool before
``MERGE``-ing data into FalkorDB. The redaction is
fail-closed: an exception or ``Err(...)`` result means the
caller MUST NOT persist; emit ``pii.check_failed`` to the
EventLog + DLQ.

Three redaction levels
----------------------

| Level | Mechanism | Catches | Cost |
|-------|-----------|---------|------|
| 1 (default) | Regex (PT + EN) | CPF, CNPJ, e-mail, telefone, CEP, chave PIX | < 1ms |
| 2 (opt-in) | ``EntityExtractor`` (heuristic OR GLiNER2-based) | + NER-derived names/addresses | ~20ms |
| 3 (opt-in) | GLiNER2 v1.5 with ``task="pii"`` (async batch audit) | + remaining semantic PII | batch |

The level is set by the constructor (``level=1`` default)
or by the env ``FMH_PII_LEVEL``. The default label set
covers the most common PII in Brazilian fiscal / ERP
data; tenants override via ``fmh:tenant:{cnpj}:pii_labels``
(Fase 4 wiring).

Idempotency
-----------

The redaction is **idempotent per payload**: re-running
on an already-redacted payload is a no-op (regex does
not match placeholders; the EntityExtractor would skip
the canonical form). The ``idempotency_key`` injected by
the ``ToolInvoker`` is the caller's stable handle; this
tool does not cache.

Why a Tool, not a filter
------------------------

ADR-006 (Tool × Role) — the decision of what is PII is
a privacy policy of the application. It may vary per
tenant, per regulation (LGPD vs. HIPAA), per event
type. Tools = plugable, configurable per deployment,
testable in isolation. Roles = semantic specialisations
on top of tools.

The redaction tool can be replaced wholesale by a
tenant's custom implementation without touching the
framework.

Package layout
--------------

* ``_patterns`` — regex patterns, ``PATTERNS`` order
  map, ``DEFAULT_PII_LABELS``.
* ``_level1`` — pure-logic level-1 (regex) redaction:
  ``redact_string``, ``redact_value``.
* ``_level2`` — async level-2 (NER) redaction:
  ``ner_redact``, plus tree walker helpers
  (``collect_strings``, ``set_at_path``).
* ``_result`` — ``RedactionResult`` dataclass.
* ``_tool`` — ``PiiRedactionTool`` (the orchestrator).
"""

from __future__ import annotations

from kntgraph.agents.tools.pii._patterns import DEFAULT_PII_LABELS
from kntgraph.agents.tools.pii._result import RedactionResult
from kntgraph.agents.tools.pii._tool import PiiRedactionTool

__all__ = [
    "DEFAULT_PII_LABELS",
    "PiiRedactionTool",
    "RedactionResult",
]
