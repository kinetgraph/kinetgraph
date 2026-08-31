# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``StructuredExtractionTool`` — the Tool surface for the
fourth GLiNER2-backed path.

ADR-055 §2.6. A consumer of the framework's GLiNER2 stack
that takes a piece of text and an **inline** schema and
returns a list of JSON-shaped records. The use case is the
PDF-OCR path (RG, CNH, NF-e, sindicato registration):
the application knows the schema it wants — RG has
``nome``, ``cpf``, ``data_nascimento``; NF-e has
``valor_total``, ``cnpj_emitente``, ``data_emissao`` —
and it passes that schema per call. No central catalog,
no per-schema registration.

The Tool does NOT load the model itself — the application
constructs an :class:`SLMStructuredExtractor` ( which in
turn uses :class:`GlinerModelRegistry` so the model is
shared with the other GLiNER2-backed adapters) and
injects the facade here. The Tool's job is the Tool
Protocol contract:

  1. Validate the schema (regex spot-check, not a full
     JSON-Schema validation — the schema dialect is
     GLiNER2's, not the Tool's).
  2. Delegate to the facade.
  3. Return :class:`Result[list[dict], ToolError]`.

Failure modes are typed:

  - ``Err(ToolError("invalid_schema: ..."))`` when the
    schema fails the spot-check.
  - ``Err(ToolError("extraction_failed: ..."))`` when the
    model raises (PyTorch, OOM, tokeniser error).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from kntgraph.agents.tools.protocol import Tool, ToolArgValue
from kntgraph.core.result import Err, Ok, Result, ToolError

if TYPE_CHECKING:
    from kntgraph.knowledge.extraction._slm_facades import (
        SLMStructuredExtractor,
    )


# Regex spot-check for a GLiNER2 field specification.
# Acceptable shapes:
#   "field_name::type"
#   "field_name::type::anchor text here"
#   "field_name"                    (no type — defaults to "str")
# where ``type`` is one of str / int / float / bool / list
# and ``anchor`` is free-form prose.
_FIELD_SPEC = re.compile(
    r"""
    ^[A-Za-z_][A-Za-z0-9_]*         # field name (Python identifier-ish)
    (?:                             # optional type and anchor
        ::
        (?: str | int | float | bool | list )
        (?: :: .+ )?                # optional anchor prose
    )?
    $
    """,
    re.VERBOSE,
)

# Pattern for a top-level schema key. GLiNER2 accepts any
# non-empty string as a structure name; we just enforce
# "non-empty, no whitespace" — enough to catch typos
# like an empty dict.
_STRUCTURE_KEY = re.compile(r"^[^\s]{1,64}$")


class StructuredExtractionTool(Tool):
    """
    Extract structured records from text using an inline schema.

    Implements the :class:`kntgraph.tools.protocol.Tool`
    Protocol. The single capability is
    ``invoke(idempotency_key=..., text=..., schema=...)``
    — the schema is inline in the request, not looked up
    in a catalog.

    Args:
      extractor: the :class:`SLMStructuredExtractor` facade
        that does the actual extraction. The Tool does
        NOT load the model — the application is
        responsible for constructing the facade (which
        in turn uses :class:`GlinerModelRegistry` so the
        model is shared with the other GLiNER2-backed
        adapters in the same process).

    Schema validation
    -----------------

    The Tool validates the schema with a regex
    spot-check, not a full JSON-Schema validation:

      - The schema is a non-empty dict.
      - It has at least one top-level key.
      - Each top-level key is a non-empty, non-whitespace
        string (the structure name).
      - The value of each key is a list of non-empty
        strings, each matching :data:`_FIELD_SPEC` (a
        Python identifier followed by an optional
        ``::type::anchor`` tail).

    Anything outside those rules becomes
    ``Err(ToolError("invalid_schema: ..."))``. The regex
    is permissive on purpose — the schema dialect is
    GLiNER2's, and the framework passes it through to
    the model verbatim. Catching the obvious typos
    (empty list, empty key, missing field name) is the
    Tool's job; everything else is the model's.
    """

    name: str = "extract_structured"
    description: str = (
        "Extract structured records from text against an inline "
        "GLiNER2 schema. The schema is opaque to the framework; "
        "the adapter passes it verbatim to the model. Returns a "
        "list of records (possibly empty, possibly more than one — "
        "e.g. multiple invoices in the same text)."
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "description": ("The source text to extract records from."),
            },
            "schema": {
                "type": "object",
                "description": (
                    "The GLiNER2 schema — a dict mapping a structure "
                    "name to a list of ``field::type[::anchor]`` "
                    "specs. Example: "
                    "``{'documento': ['nome::str', 'cpf::str']}``."
                ),
            },
        },
        "required": ["text", "schema"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        extractor: "SLMStructuredExtractor",
    ) -> None:
        """Bind the facade.

        The Tool holds a single dependency (the
        :class:`SLMStructuredExtractor`). Construction
        does NOT load the model — the facade does that
        on its own construction, so the model is
        loaded once per process regardless of how
        many Tools are wired.
        """
        self._extractor = extractor

    async def invoke(  # type: ignore[reportIncompatibleMethodOverride]
        self,
        *,
        idempotency_key: str,
        text: str,
        schema: "dict[str, object]",
        **kwargs: "ToolArgValue",
    ) -> Result[list[dict[str, object]], ToolError]:
        """
        Tool Protocol entry point.

        Validates the schema (regex spot-check),
        delegates to the facade, and surfaces failures
        as :class:`ToolError` with a stable prefix
        (``invalid_schema:`` or ``extraction_failed:``).

        ``idempotency_key`` is required by the Protocol
        but unused (the extraction is itself a pure
        function of ``(text, schema)``). The argument
        is accepted so the Tool can be invoked via the
        standard ``ToolInvoker``.
        """
        # Schema validation happens before the model
        # call — a bad schema is the caller's bug, not
        # the model's. Fail fast with a clear prefix
        # so the DLQ can route the failure to the right
        # triage bucket.
        schema_error = _validate_schema(schema)
        if schema_error is not None:
            return Err(ToolError(f"invalid_schema: {schema_error}"))

        try:
            records = await self._extractor.extract(text, schema)
        except Exception as e:  # noqa: BLE001
            # Fail-closed: never raise. Surface the
            # error as ``Err(ToolError)``. The Tool
            # Protocol's failure prefix is stable so
            # the DLQ / replay path can branch on it
            # without parsing the message.
            return Err(ToolError(f"extraction_failed: {e!r}"))

        return Ok(records)


def _validate_schema(schema: object) -> "str | None":
    """
    Return a human-readable error string when the schema
    fails the spot-check, or ``None`` when it is valid.

    The regex checks are permissive on purpose: the
    schema dialect is GLiNER2's, and the framework passes
    the dict through to the model verbatim. The Tool's
    job is to catch the obvious typos (empty dict, empty
    list, empty field name, non-string spec). The model
    itself will surface dialect-level errors at call
    time.
    """
    if not isinstance(schema, dict):
        return "schema must be a dict"
    if not schema:
        return "schema must have at least one structure"
    for structure_name, fields in schema.items():
        err = _validate_structure(structure_name, fields)
        if err is not None:
            return err
    return None


def _validate_structure(structure_name: object, fields: object) -> "str | None":
    """Validate one structure of the schema. Returns an
    error string when the structure or any of its field
    specs fails the spot-check, or ``None`` when valid."""
    if not isinstance(structure_name, str) or not _STRUCTURE_KEY.match(structure_name):
        return (
            f"structure name must be a non-empty "
            f"non-whitespace string; got {structure_name!r}"
        )
    if not isinstance(fields, list) or not fields:
        return f"structure {structure_name!r} must be a non-empty list of field specs"
    for spec in fields:
        if not isinstance(spec, str) or not _FIELD_SPEC.match(spec):
            return (
                f"structure {structure_name!r} has an "
                f"invalid field spec {spec!r}; expected "
                f"'field' or 'field::type[::anchor]'"
            )
    return None


__all__ = ["StructuredExtractionTool"]
