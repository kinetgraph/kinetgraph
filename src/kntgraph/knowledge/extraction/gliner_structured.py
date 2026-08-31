# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
GlinerStructuredAdapter — GLiNER2-backed structured extractor.

ADR-055 §2.4. The fourth consumer of the framework's GLiNER2
stack (alongside entity / intent / argument extraction). Takes
a piece of text and an inline schema and returns a list of
JSON-shaped records — one entry per instance the model found
(e.g. multiple invoices in the same bundle; one RG; one CNH).

The schema dialect
------------------

The schema is the **GLiNER2 native dialect**, NOT the JSON
Schema consumed by the argument path. The shape is::

    {
        "<structure_name>": ["field::type", "field::type::anchor", ...],
    }

where ``type`` is one of ``str``, ``int``, ``float``, ``bool``,
``list`` and the optional ``anchor`` is a description that
biases the field resolution. The framework passes the schema
through to ``model.extract_json`` verbatim — the adapter does
not validate the dialect (the Tool does a regex spot-check
upstream; full validation is upstream's job).

Output shape
------------

``GLiNER2.extract_json`` returns::

    {"<structure_name>": [{"field": ..., ...}, ...]}

The adapter flattens the wrapper: it returns just the inner
list of records (one entry per instance). Each record is a
``dict[str, object]``. When ``include_confidence=True``, the
per-field confidence is preserved as a sibling key
``__confidence_<field>`` on the same record; off by default
to keep the output clean for the Tool caller.

field_threshold
---------------

The upstream ``extract_json`` API does not accept per-field
thresholds. The adapter applies the threshold **post-call**
in pure Python: a field whose confidence is below
``field_threshold`` is dropped from the record before the
record is returned. The threshold mirrors
:class:`kntgraph.knowledge.extraction.argument.SchemaArgumentExtractor.field_threshold`.

Threading
---------

``GlinerModelRegistry.get`` (ADR-055 Fase 1) loads the model
once and caches it for the process lifetime. ``model.extract_json``
is a PyTorch call; the adapter wraps the inference in
``asyncio.to_thread`` so the event loop stays responsive
under a ``ToolInvoker`` workload.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ._gliner_model_registry import GlinerModelRegistry
from .base import StructuredExtractor

if TYPE_CHECKING:
    pass


class GlinerStructuredAdapter(StructuredExtractor):
    """
    Low-level GLiNER2-backed structured extractor.

    Implements :class:`StructuredExtractor` against the
    GLiNER2 ``extract_json`` primitive. Construction
    resolves the model name from the explicit argument or
    ``Settings.arg_extractor_model_id`` and acquires the
    model through :class:`GlinerModelRegistry` (ADR-055
    Fase 1) so deployments that also wire entity / intent
    / argument paths share a single loaded checkpoint.

    Args:
      model_name: HuggingFace repo id or local path. When
        ``None`` (default), reads ``Settings.arg_extractor_model_id``.
        ``None`` lets GLiNER2 pick (CPU when no accelerator
        is available).
      device: PyTorch device string (``"cpu"``, ``"cuda"``).
      field_threshold: minimum per-field confidence to keep
        a field in the returned record. Fields below the
        threshold are dropped silently. Mirrors
        :class:`SchemaArgumentExtractor.field_threshold`.
      include_confidence: when ``True``, the per-field
        confidence returned by GLiNER2 is preserved as a
        sibling key ``__confidence_<field>`` on the
        record. Off by default.
    """

    def __init__(
        self,
        model_name: str | None = None,
        *,
        device: str | None = None,
        field_threshold: float = 0.5,
        include_confidence: bool = False,
    ) -> None:
        """Construct the adapter and acquire the model.

        Resolves the model name (explicit arg or
        ``Settings.arg_extractor_model_id``) and asks the
        registry for the cached instance. The registry
        raises ``ImportError`` (via ``require_optional``)
        when ``gliner2`` is not installed, with a message
        pointing to ``kntgraph[gliner]``.
        """
        model_name = self._resolve_model_name(model_name)

        # ADR-055 Fase 1: route through the registry so
        # every GLiNER2-backed adapter in this process
        # shares one loaded checkpoint.
        from kntgraph.infra.config import fresh_settings

        cache_dir = fresh_settings().model_cache_dir
        self._model = GlinerModelRegistry.get(
            model_name,
            device=device,
            cache_dir=cache_dir,
        )
        # Bound the threshold to [0, 1] defensively — the
        # adapter applies it post-hoc, but a negative or
        # > 1 value would silently keep/drop every field.
        self._field_threshold = float(field_threshold)
        self._include_confidence = bool(include_confidence)
        self._model_name = model_name

    @staticmethod
    def _resolve_model_name(model_name: "str | None") -> str:
        """
        Resolve the effective model name from explicit arg
        + ``Settings``.

        ``None`` means "no override; use ``Settings``".
        Any explicit value wins. Encapsulated so the
        ``__init__`` body stays flat (CC ≤ 2) and the
        defaults are easy to test in isolation.
        """
        if model_name is not None:
            return model_name
        from kntgraph.infra.config import fresh_settings

        return fresh_settings().arg_extractor_model_id

    @property
    def model_name(self) -> str:
        """Return the resolved model name (HF repo id or path)."""
        return self._model_name

    @property
    def field_threshold(self) -> float:
        """Return the field-level confidence floor in [0, 1]."""
        return self._field_threshold

    @property
    def include_confidence(self) -> bool:
        """Return whether the per-field confidence is preserved."""
        return self._include_confidence

    async def extract(
        self,
        text: str,
        schema: "dict[str, object]",
    ) -> "list[dict[str, object]]":
        """
        Extract structured records from ``text`` against ``schema``.

        Wraps the blocking ``model.extract_json`` call in
        ``asyncio.to_thread`` and flattens the upstream
        ``{structure: [record, ...]}`` wrapper into a
        plain list of records.

        Per-field filtering happens post-call: a field
        whose confidence is below ``self._field_threshold``
        is dropped from the record (the ``__confidence_*``
        sibling is dropped with it).

        A record with no fields left after filtering is
        dropped from the result list (a model that finds
        only low-confidence matches returns ``[]``, not
        ``[{}]``).
        """
        # Capture the bound values so the closure sees the
        # state at call time, not whatever a later
        # attribute assignment might replace them with.
        threshold = self._field_threshold
        include_confidence = self._include_confidence
        model = self._model

        raw = await asyncio.to_thread(
            model.extract_json,
            text,
            schema,
            # The ``include_confidence`` flag is what lets us
            # apply the threshold; the API takes it once,
            # at call time, and our kwargs forward through.
            include_confidence=True,
        )

        records = self._normalise(raw, threshold, include_confidence)
        return records

    @staticmethod
    def _normalise(
        raw: object,
        threshold: float,
        include_confidence: bool,
    ) -> "list[dict[str, object]]":
        """
        Convert ``GLiNER2.extract_json`` output to a list of records.

        The upstream returns
        ``{<structure>: [{"field": ..., ...}, ...]}`` where
        each field value is either a bare value (when
        ``include_confidence`` is False) or
        ``{"text": ..., "confidence": ...}`` when True.

        This helper:

          1. Unwraps the single top-level structure key
             (the only shape the framework promises; the
             Tool's regex spot-check enforces it upstream).
          2. Filters out fields whose confidence is below
             ``threshold`` (when ``include_confidence`` is
             True — when False, every field is kept).
          3. When ``include_confidence`` is True, flattens
             ``{"text": x, "confidence": y}`` into
             ``x`` plus a sibling ``__confidence_x_field``
             key on the same record.
          4. Drops records that have no fields left after
             filtering.

        Defensive against the upstream returning ``None`` or
        an unexpected shape — bad output becomes ``[]``,
        not an exception.
        """
        if not isinstance(raw, dict):
            # The upstream may return ``None`` when the
            # model finds nothing (small checkpoints do).
            # An unexpected shape is treated the same way:
            # empty result, no crash. The Tool surfaces the
            # ``[]`` as-is to the caller.
            return []

        # Unwrap the structure key. The Tool's regex
        # spot-check guarantees exactly one key, but we
        # accept multiple by concatenating (defensive
        # against a future backend that returns more).
        records: "list[dict[str, object]]" = []
        for structure_value in raw.values():
            if not isinstance(structure_value, list):
                continue
            for entry in structure_value:
                if not isinstance(entry, dict):
                    continue
                filtered = _filter_record(
                    entry,
                    threshold=threshold,
                    include_confidence=include_confidence,
                )
                if filtered is not None:
                    records.append(filtered)
        return records


def _filter_record(
    entry: "dict[str, object]",
    *,
    threshold: float,
    include_confidence: bool,
) -> "dict[str, object] | None":
    """
    Apply the per-field confidence threshold to one record.

    Returns the filtered record, or ``None`` when every
    field was dropped (caller skips the record in that
    case so the result stays a clean ``list[dict]``
    rather than ``list[dict_containing_no_fields]``).

    The upstream always returns ``{"text": x, "confidence": y}``
    per field — the adapter requests ``include_confidence=True``
    unconditionally so the threshold can be applied. This
    helper unwraps that shape in both cases: when the
    caller asked for confidence to be preserved (the
    ``__confidence_*`` sibling key is added), or when the
    caller asked for a clean shape (the confidence is
    discarded after the threshold check).
    """
    out: "dict[str, object]" = {}
    for field_name, field_value in entry.items():
        # Upstream returns ``{"text": x, "confidence": y}``
        # per field when ``include_confidence`` was set at
        # model call time (which we always do). Unwrap
        # the dict and apply the threshold against the
        # ``confidence`` member.
        if isinstance(field_value, dict):
            text_value = field_value.get("text", field_value)
            confidence = field_value.get("confidence")
            if isinstance(confidence, (int, float)) and float(confidence) < threshold:
                # Drop the field; the caller never sees a
                # low-confidence guess in a side-effecting
                # Tool payload.
                continue
            out[field_name] = text_value
            # Preserve the confidence as a sibling key
            # only when the caller asked for it
            # (``include_confidence=True``).
            if include_confidence and isinstance(confidence, (int, float)):
                out[f"__confidence_{field_name}"] = float(confidence)
            continue

        # Bare-value shape (a future backend that does
        # not emit confidence). Keep the field verbatim —
        # there is no confidence to threshold against.
        out[field_name] = field_value

    if not out:
        return None
    return out


__all__ = ["GlinerStructuredAdapter"]
