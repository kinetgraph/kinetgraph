# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``SLM*`` facades — public surfaces over GLiNER2-backed
extraction adapters (Iter 21).

The three facades:

  - :class:`SLMEntityExtractor`     — entity extraction
  - :class:`SLMIntentClassifier`    — intent classification
  - :class:`SLMArgumentExtractor`   — argument extraction

Why three facades (and not one)
-------------------------------

GLiNER2 is a single zero-shot NLU model, but the **three
extraction tasks have different contracts**:

  - ``EntityExtractor``  — "which spans in the text
    match a label?" (many matches per call).
  - ``IntentClassifier`` — "which of K labels best
    describes the WHOLE text?" (one decision per call).
  - ``ArgumentExtractor`` — "given a Tool's input schema,
    what value fills each field?" (schema-driven, per-tool).

The contracts are documented in :mod:`.base` (entity +
intent) and :mod:`kntgraph.agents.knowledge.argument_extractor`
(argument). The framework's policy (§13.1 — Simplicidade)
favors one type per concern, so the three facades stay
separate.

Why ``SLM`` prefix
------------------

``SLM`` stands for "Small Language Model". The prefix
signals that the facade is a thin wrapper over a local
language model — not a remote LLM call. It is NOT tied
to GLiNER2 in particular: the default implementation is
``GlinerEntityAdapter`` / ``GlinerIntentAdapter`` /
``GlinerArgumentAdapter``, but a future
``TinyLLMEntityAdapter`` can be slotted in via
``SLMEntityExtractor(adapter=...)`` without changing
the facade's public API.

Why a facade (and not just the adapter)
---------------------------------------

  - The adapter is a "low-level" object that pins the
    model backend. Apps want to say "I want an SLM-based
    extractor" without committing to GLiNER2 — they get
    a swappable point of injection.
  - The facade IS-A Protocol (``SLMEntityExtractor`` is an
    ``EntityExtractor``). Code that consumes the
    Protocol is decoupled from the concrete adapter.
  - The facade makes the factory import lazy. When the
    ``gliner2`` package is missing, ``SLMEntityExtractor()``
    surfaces a clear error via the adapter, NOT an
    import-time crash on the rest of the framework.

Iter 27: the three default adapters live in
:mod:`kntgraph.knowledge.extraction` directly:

  - :class:`GlinerEntityAdapter` (in ``gliner.py``)
  - :class:`GlinerIntentAdapter` (in ``gliner_intent.py``)
  - :class:`GlinerArgumentAdapter` (in ``gliner_argument.py``)

The facade imports the default adapter **locally**
inside ``__init__`` (when the caller did not supply
``adapter=``). This keeps the facade importable
without loading the underlying GLiNER2 model, AND
preserves the framework's purity: there is no
``kntgraph → kntgraph.agents`` leak in the
``knowledge.extraction`` package.

Usage
-----

.. code-block:: python

    from kntgraph.knowledge.extraction import (
        SLMEntityExtractor,
        SLMIntentClassifier,
    )

    entity_extractor = SLMEntityExtractor()       # GlinerEntityAdapter
    intent = SLMIntentClassifier()                # GlinerIntentAdapter
    arg_extractor = SLMArgumentExtractor(registry)  # GlinerArgumentAdapter

    # Or inject a custom adapter (future-proof):
    # entity_extractor = SLMEntityExtractor(adapter=MyLocalNERAdapter(...))
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional, cast

from .base import Classification, Entity
from .gliner import DEFAULT_LABELS, GlinerEntityAdapter
from .gliner_intent import GlinerIntentAdapter
from kntgraph.knowledge.extraction.base import (
    ArgExtraction,
    ArgumentExtractor,
    EntityExtractorWithMentions,
    IntentClassifier,
)

if TYPE_CHECKING:
    from kntgraph.tools.registry import ToolRegistry


class SLMEntityExtractor(EntityExtractorWithMentions):
    """
    Public facade for SLM-based entity extraction (Iter 21).

    IS-A :class:`EntityExtractorWithMentions` (and therefore
    :class:`EntityExtractor` — the broader Protocol is
    satisfied because ``EntityExtractorWithMentions``
    extends it). Holds a reference to a low-level adapter
    (default: :class:`GlinerEntityAdapter`) and delegates
    every call.

    Why a facade over a concrete subclass:

      - The adapter is a *template method* (subclass-and-
        wire). Most apps want a working default — the
        facade ships the no-op base adapter. Apps that
        have a real GLiNER2 deployment inject their own
        subclass.
      - Apps that need a different local model (e.g. a
        Portuguese fiscal NER) inject a custom adapter.
        The facade IS-A Protocol so the rest of the
        framework doesn't change.
      - Construction-side effects (e.g. the lazy ``gliner2``
        import error) surface on ``__init__`` of the
        adapter, with a clear remediation message — not
        an opaque ``ImportError`` at framework boot.
    """

    def __init__(
        self,
        *,
        adapter: Optional[EntityExtractorWithMentions] = None,
        labels: tuple[str, ...] | None = None,
        threshold: float = 0.5,
    ) -> None:
        """
        Args:
          adapter: the low-level adapter that actually
            does the work. When ``None`` (default), the
            facade instantiates :class:`GlinerEntityAdapter`
            with the provided ``labels`` and ``threshold``.
          labels: forwarded to the default adapter.
            Ignored when ``adapter`` is supplied.
          threshold: forwarded to the default adapter.
            Ignored when ``adapter`` is supplied.
        """
        if adapter is not None:
            self._adapter = adapter
        else:
            self._adapter = GlinerEntityAdapter(
                labels=labels if labels is not None else DEFAULT_LABELS,
                threshold=threshold,
            )

    async def extract(self, text: str) -> list[Entity]:
        return await self._adapter.extract(text)

    async def extract_with_mentions(
        self, text: str
    ) -> list[tuple[Entity, Optional[int]]]:
        return await self._adapter.extract_with_mentions(text)


class SLMIntentClassifier(IntentClassifier):
    """
    Public facade for SLM-based intent classification (Iter 21).

    IS-A :class:`IntentClassifier`. Holds a reference to a
    low-level adapter (default: :class:`GlinerIntentAdapter`)
    and delegates every call.

    Construction loads the model eagerly (delegated to the
    default adapter). For deployments that need lazy
    loading, inject an already-constructed adapter via
    ``adapter=`` and wrap construction in a startup hook
    (e.g. an ``async def warmup()`` in the role).
    """

    def __init__(
        self,
        *,
        adapter: Optional[IntentClassifier] = None,
        model_name: str | None = None,
        device: Optional[str] = None,
        threshold: float = 0.5,
    ) -> None:
        """
        Args:
          adapter: the low-level adapter that actually
            does the work. When ``None`` (default), the
            facade instantiates :class:`GlinerIntentAdapter`
            with the provided ``model_name`` (or Settings
            default), ``device`` and ``threshold``.
          model_name: forwarded to the default adapter.
            Ignored when ``adapter`` is supplied. ``None``
            means "use ``Settings.arg_extractor_model_id``".
          device: forwarded to the default adapter.
          threshold: forwarded to the default adapter.
        """
        if adapter is not None:
            self._adapter = adapter
        else:
            self._adapter = GlinerIntentAdapter(
                model_name=model_name,
                device=device,
                threshold=threshold,
            )

    @property
    def model_name(self) -> str:
        return self._adapter.model_name  # type: ignore[no-any-return]

    async def classify(
        self,
        text: str,
        labels: Iterable[str],
        descriptions: Optional[Iterable[str]] = None,
    ) -> Classification:
        # ``self._adapter`` is typed as ``IntentClassifier``,
        # whose ``classify`` accepts 2 args. The default
        # adapter (``GlinerIntentAdapter``) extends the
        # Protocol with an optional ``descriptions`` kwarg;
        # the cast pins the wider signature here so the
        # facade can forward descriptions transparently.
        # The cast is sound: the Protocol return shape is
        # ``Classification`` (same as the concrete adapter's)
        # and the runtime always resolves to the concrete
        # adapter (or to a test double that mirrors the
        # contract).
        return await cast("GlinerIntentAdapter", self._adapter).classify(
            text, labels, descriptions
        )


class SLMArgumentExtractor(ArgumentExtractor):
    """
    Public facade for SLM-based argument extraction (Iter 21).

    IS-A :class:`ArgumentExtractor`. Holds a reference to a
    low-level adapter (default: :class:`GlinerArgumentAdapter`)
    and delegates every call.

    Construction loads the model eagerly (delegated to the
    default adapter's underlying :class:`GlinerFieldFinder`).
    For deployments that need lazy loading, inject an
    already-constructed adapter.

    The facade's `__init__` does NOT take ``model_name``
    directly — it forwards the arg to the default adapter.
    The adapter reads ``Settings.arg_extractor_model_id``
    when the arg is ``None`` (Iter 21). Tests that need
    to override the model build a custom adapter and
    inject it.
    """

    def __init__(
        self,
        registry: "ToolRegistry",
        *,
        adapter: Optional[ArgumentExtractor] = None,
        model_name: str | None = None,
        device: Optional[str] = None,
        field_threshold: float = 0.5,
    ) -> None:
        """
        Args:
          registry: forwarded to the default adapter.
            Ignored when ``adapter`` is supplied.
          adapter: the low-level adapter. When ``None``
            (default), the facade instantiates
            :class:`GlinerArgumentAdapter` (the framework
            default).
          model_name: forwarded to the default adapter.
            ``None`` means "use ``Settings.arg_extractor_model_id``".
          device: forwarded to the default adapter.
          field_threshold: forwarded to the default adapter.
        """
        if adapter is not None:
            self._adapter = adapter
        else:
            # Iter 27: the default adapter now lives in
            # the framework (``kntgraph.knowledge
            # .extraction.gliner_argument``). The
            # adapter's ``__init__`` does lazy local
            # imports of the vertical pieces; the
            # framework import itself does NOT load
            # the vertical.
            from kntgraph.knowledge.extraction import (
                GlinerArgumentAdapter,
            )

            self._adapter = GlinerArgumentAdapter(
                registry,
                model_name=model_name,
                device=device,
                field_threshold=field_threshold,
            )

    @property
    def model_name(self) -> str:
        return self._adapter.model_name  # type: ignore[attr-defined]

    async def extract(self, text: str, tool_name: str) -> ArgExtraction:
        return await self._adapter.extract(text, tool_name)


__all__ = [
    "SLMEntityExtractor",
    "SLMIntentClassifier",
    "SLMArgumentExtractor",
]
