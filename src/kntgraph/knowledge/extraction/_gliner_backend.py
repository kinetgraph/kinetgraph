# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
#

"""
_gliner_backend — the GLiNER2 backend contract, and the
choice the application makes about it.

The framework pins the *shape* of the backend (what the
adapters call), never the *runtime* that serves it.

Why the runtime is the application's
------------------------------------
``gliner2`` is the adapter library; the model runs on
``torch``, which ``gliner2`` does **not** depend on — it
lives in the ``local`` extra of that package. So
``uv add "kntgraph[gliner]"`` gives the adapter, not a
runnable model, and the operator still has to choose:

  - **local inference** — ``uv add "gliner2[local]"``
    brings ``torch``. The framework does not pull it: the
    default ``torch`` wheel on Linux is the CUDA build, and
    a library that drags gigabytes of GPU wheels into every
    install is a library nobody installs.

  - **a remote or alternative backend** — the application
    already has a client (an HTTP service, an ONNX
    runtime, a TinyLLM). It implements the Protocol below
    and injects it; no ``gliner2``, no ``torch``, nothing
    local to install.

  - **no model at all** — the default extractors are
    heuristic (regex + payload-key) and need no extra.

The two Protocols
-----------------
:class:`IntentModel` is what the intent-classification
adapter calls; :class:`FieldFinderModel` is what the
argument-extraction finder calls. They are separate because
GLiNER2 exposes two different surfaces and the framework
does not want to force one shape onto both.

Both are ``Protocol``\\s, not base classes: the application
implements them structurally, and a local ``GLiNER2``
instance satisfies them as-is.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# The framework extra that ships the GLiNER2 *adapter*.
GLINER2_EXTRA = "kntgraph[gliner]"

# What to do when ``gliner2`` is installed but its inference
# runtime is not. The wording is shared by every call site
# so the operator reads the same remedy wherever the error
# comes from.
GLINER2_RUNTIME_HINT = (
    "The inference runtime is the application's choice:\n"
    '    uv add "gliner2[local]"      # local inference (torch)\n'
    '    pip install "gliner2[local]"\n'
    "Or skip the local runtime entirely — inject the model you\n"
    "already have (see `IntentModel` / `FieldFinderModel`)."
)


@runtime_checkable
class MatchDict(Protocol):
    """Structural shape of a GLiNER2 dict-shaped match.

    GLiNER2 1.3.x with ``include_confidence=True`` returns
    matches as ``{"text": str, "confidence": float}`` (or
    ``"score"`` / ``"surface"`` / ``"value"`` aliases). The
    Protocol is for static typing only; the helpers
    duck-type at runtime.
    """

    text: str
    confidence: float


@runtime_checkable
class MatchObj(Protocol):
    """Structural shape of a GLiNER2 dataclass-shaped match.

    GLiNER2 pre-1.3 returns dataclass instances with
    ``.text`` and ``.score`` attributes. The Protocol is
    for static typing only; ``@runtime_checkable`` lets
    the candidate-list helper narrow the union without
    having to import the upstream type at module level.
    """

    text: str
    score: float


# A single match from a GLiNER2 entities result is one of:
#   - a bare string (the default, with ``include_confidence=False``);
#   - a dict with ``text`` / ``confidence`` (1.3.x canonical);
#   - a dataclass with ``.text`` / ``.score`` (pre-1.3).
GlinerMatch = str | MatchDict | MatchObj

# The raw GLiNER2 ``.extract_entities(...)`` response is
# a nested dict: ``{"entities": {label: [match, ...]}}``
# (1.3.x). Older versions return a list of dataclasses
# directly. The framework reads it through its private
# ``_read`` helper so the exact shape is tolerated; this
# alias exists for the call sites that bind the result.
GlinerRawResult = dict[str, Any] | list[GlinerMatch]


@runtime_checkable
class IntentModel(Protocol):
    """
    The model surface the intent-classification adapter calls.

    A local ``gliner2.GLiNER2`` instance satisfies this as-is.
    A remote client satisfies it too: the adapter never
    assumes the model runs in this process.

    The parameter names mirror ``gliner2``'s own signature
    (``texts`` / ``schemas``) so a real model satisfies the
    Protocol structurally, without a cast.
    """

    def batch_extract(
        self,
        texts: list[str],
        schemas: dict[str, object],
        *,
        include_confidence: bool = False,
    ) -> list[dict[str, object]]:
        """Classify a batch of texts against a task schema."""
        ...


@runtime_checkable
class FieldFinderModel(Protocol):
    """
    The model surface the argument-extraction finder calls.

    The return shape is the GLiNER2 one — a nested dict
    (``{"entities": {label: [match, ...]}}``) or the older
    list of matches — so that the parsing helpers
    (:func:`~kntgraph.knowledge.extraction.argument.extract_first`)
    keep working with a local model and with a remote one.

    The parameter names mirror ``gliner2``'s own signature
    (``text`` / ``entity_types``) so a real model satisfies
    the Protocol structurally, without a cast.
    """

    def extract_entities(
        self,
        text: str,
        entity_types: list[str],
        *,
        include_confidence: bool = False,
    ) -> GlinerRawResult:
        """Extract the entities matching ``entity_types`` in ``text``."""
        ...


__all__ = [
    "GLINER2_EXTRA",
    "GLINER2_RUNTIME_HINT",
    "FieldFinderModel",
    "GlinerMatch",
    "GlinerRawResult",
    "IntentModel",
    "MatchDict",
    "MatchObj",
]
