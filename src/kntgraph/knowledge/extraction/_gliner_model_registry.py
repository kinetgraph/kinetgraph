# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
GlinerModelRegistry — process-level singleton for loaded GLiNER2 instances.

ADR-055 §2.1. The four GLiNER2-backed adapters in the framework
(``GlinerEntityAdapter``'s subclasses, ``GlinerIntentAdapter``,
``GlinerFieldFinder``, ``GlinerStructuredAdapter``) used to call
``GLiNER2.from_pretrained`` directly in their constructors. In a
deployment that runs all four with the same checkpoint, that meant four
cold starts and four copies of the same weights in RAM (~500 MB each
for the 205M base model, ~300 MB on the 74M 2.5-small variant).

This module provides a single class-level dict keyed by
``(model_name, device, cache_dir)`` so the first caller pays the load
cost and every subsequent caller does an O(1) dict lookup.

The loader is ``gliner2.AutoExtractor`` (not ``gliner2.GLiNER2``).
``AutoExtractor`` dispatches by the checkpoint's saved ``architecture``
field — gliner2 2.0 (span) returns a ``GLiNER2`` instance, gliner2 2.5
(boundary) returns a ``BoundaryExtractor``. Both expose the same
public API (``extract_json``, ``extract_entities``, ``classify_text``,
``batch_extract_*``), so the rest of the framework stays agnostic to
the underlying architecture.

Thread-safety
-------------

The GIL protects the dict read. Two threads racing on the first call
for the same key will both enter the ``if key not in cls._cache`` branch
and both call ``from_pretrained``. PyTorch serialises concurrent loads
of the same checkpoint via a file lock on the HuggingFace cache; the
second thread blocks until the first finishes, then stores its own
instance. The second instance is correctness-neutral — two copies of the
same weights produce identical output. The duplicate-load window is one
disk read; V1 accepts this. A future iter can add a per-key
``threading.Lock`` if profiling shows a measurable cost.

Cache key
---------

The key is ``(model_name, device, cache_dir)`` so:

- Same ``model_name``, different ``device`` → separate instances (the
  model is device-bound after the first forward pass).
- Same ``model_name`` and ``device``, different ``cache_dir`` → separate
  entries (different local copies, potentially different weights).
- All three equal → same instance.

No eviction
-----------

The cache holds instances for the life of the process. Multi-tenant LRU
eviction (``GLiNER2ModelPool``) is a pending concern documented in
ADR-055 §4 and is not implemented here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # GLiNER2 is an optional dependency (``kntgraph[gliner]``).
    # The type annotation is under TYPE_CHECKING so the module
    # is importable even when ``gliner2`` is not installed. The
    # runtime guard is inside ``get``, where ``require_optional``
    # raises a clear error if the package is absent.
    from gliner2 import GLiNER2


class GlinerModelRegistry:
    """
    Process-level cache of loaded GLiNER2 instances.

    Keyed by ``(model_name, device, cache_dir)``. The first call for a
    given key loads the model; subsequent calls return the cached
    instance. No eviction in V1.

    All adapters that need a GLiNER2 model MUST go through
    ``GlinerModelRegistry.get`` instead of calling
    ``GLiNER2.from_pretrained`` directly. This guarantees that a
    deployment running entity + argument + intent adapters with the same
    checkpoint pays the cold start once, not three times.
    """

    # The class-level dict. Keys are ``(model_name, device, cache_dir)``
    # tuples; values are loaded ``GLiNER2`` instances. Class-level (not
    # instance-level) so the cache survives across adapter
    # constructions within the same process.
    _cache: "dict[tuple[str, str | None, str | None], GLiNER2]" = {}

    @classmethod
    def get(
        cls,
        model_name: str,
        device: str | None = None,
        cache_dir: str | None = None,
    ) -> "GLiNER2":
        """
        Return the cached GLiNER2 instance for the given key, loading it
        on the first call.

        Args:
          model_name: HuggingFace repo id or local path, passed verbatim
            to ``GLiNER2.from_pretrained``. Examples:
            ``"gliner2-base"``, ``"/data/models/gliner2-large-v1"``.
          device: PyTorch device string (e.g. ``"cpu"``, ``"cuda"``).
            ``None`` lets GLiNER2 choose (typically CPU when no
            accelerator is available).
          cache_dir: Local directory where HuggingFace model weights are
            stored. Passed as ``cache_dir`` to ``from_pretrained``.
            ``None`` defers to the HuggingFace default
            (``~/.cache/huggingface`` or ``$HF_HOME``). Callers should
            read this from ``Settings.model_cache_dir``; the registry
            does not read Settings directly to stay dependency-free.

        Raises:
          ImportError: when the ``gliner2`` package is not installed.
            The error message points to ``kntgraph[gliner]``.
        """
        key = (model_name, device, cache_dir)
        if key not in cls._cache:
            from kntgraph._optional import require_optional

            # Load the optional dependency here, not at module level, so
            # the module is importable without ``gliner2`` installed.
            gliner2 = require_optional(
                "gliner2",
                "kntgraph[gliner]",
                purpose="GlinerModelRegistry",
            )
            # ``AutoExtractor`` dispatches to the right architecture
            # (span or boundary) from the checkpoint's saved
            # ``architecture`` field — gliner2 2.0 (span) returns a
            # ``GLiNER2`` instance, gliner2 2.5 (boundary) returns a
            # ``BoundaryExtractor``. Both expose the same public API
            # (``extract_json``, ``extract_entities``, ``classify_text``,
            # etc.), so the rest of the framework stays agnostic.
            #
            # Pass kwargs through only when set — gliner2 2.x removed
            # ``device=`` from the signature (``None`` is rejected by
            # the upstream validator), so the registry forwards only
            # what the caller explicitly provided. The cache key still
            # records the ``None`` distinction because the semantic is
            # different (no explicit override vs. explicit value).
            load_kwargs: dict[str, str] = {}
            if device is not None:
                load_kwargs["map_location"] = device
            if cache_dir is not None:
                load_kwargs["cache_dir"] = cache_dir
            cls._cache[key] = gliner2.AutoExtractor.from_pretrained(
                model_name,
                **load_kwargs,
            )
        return cls._cache[key]

    @classmethod
    def _clear(cls) -> None:
        """
        Flush the entire cache.

        Test-only. Production code must never call this — the registry
        is a process singleton by design. Tests that need isolated
        state call this in a ``@pytest.fixture(autouse=True)`` of
        function scope.
        """
        cls._cache.clear()


__all__ = ["GlinerModelRegistry"]
