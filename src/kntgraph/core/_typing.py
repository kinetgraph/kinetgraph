# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core._typing -- Framework-shared type aliases and Protocols.

Module-level types that several framework layers
(infra adapters, security adapters, transport
adapters) need in order to avoid ``Any`` at the
framework boundary. Per AGENTS.md §1.4, every
external library that crosses into the framework
must be wrapped in an adapter type defined here.

Why a separate module
---------------------

Putting these in ``kntgraph.core.__init__``
creates a cyclic risk (core already depends on
itself in many places). A private ``_typing``
sibling keeps the import surface flat: callers
do ``from kntgraph.core._typing import ...``.

What lives here
---------------

* :data:`JsonScalar` / :data:`JsonValue` -- the
  recursive Union for any JSON-serialisable value
  (events, payloads, tool kwargs). Mirrors the
  alias defined in
  :mod:`kntgraph.agents.memory.solutions._values` but
  promoted to the core so the rest of the
  framework can import without dragging in the
  Solution tier.
* :class:`OpaqueKey` -- Protocol for an opaque
  Ed25519 key handle. Used in
  :mod:`kntgraph.security.keys._types` to type
  the internal ``_key`` field of the public key
  wrappers without leaking
  ``cryptography.hazmat`` into the framework.
* :class:`RouterApp` -- Protocol for the FastAPI
  ``app`` object that the intent-router installers
  accept. Lets the installers stay FastAPI-free
  while still being statically typed.
* :data:`Dependable` / :data:`HTTPExceptionLike` /
  :data:`HeaderParam` -- minimal Protocols for the
  FastAPI primitives the installers import lazily.
* :data:`ValidatorInput` -- the legitimate edge
  type for runtime validators that accept any
  scalar (the framework enforces structure
  downstream, not at the input).
"""

from __future__ import annotations

from typing import Callable, Protocol, TypeVar, Union


# ---------------------------------------------------------------------------
# JSON-serialisable values (recursive).
# ---------------------------------------------------------------------------

JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[
    JsonScalar,
    dict[str, "JsonValue"],
    list["JsonValue"],
]


# ---------------------------------------------------------------------------
# Generic TypeVars for opaque framework handles.
#
# These TypeVars are unconstrained on purpose. The framework
# treats whatever they bind to as a black box (no member
# access, no isinstance checks). The name documents the
# intent; the lack of ``bound=`` keeps the type honest
# (no implicit ``object`` upper bound).
# ---------------------------------------------------------------------------

# An opaque Ed25519 key handle (``cryptography.hazmat``).
# The framework never introspects it; the wrappers expose
# only ``.bytes`` / ``.public_key()`` via duck typing.
KeyHandleT = TypeVar("KeyHandleT")

# A single ECS component value. The World does not know
# the component schema (that lives in the Tool / domain
# layer); it stores whatever the projection produced.
ComponentT = TypeVar("ComponentT")

# Runtime validator input. Validators at the framework
# boundary (agent_id, event_type, event data) accept any
# JSON-serialisable value and decide; the caller does not
# need to narrow before invoking them.
ValidatorInputT = TypeVar("ValidatorInputT")

# A generic opaque handle for framework-owned black-box
# objects (e.g. the underlying FalkorDB client managed by
# ``LiteGraphPool``). The framework treats the bound
# type as opaque and only exposes a typed projection
# (``KeyHandleT`` for Ed25519).
OpaqueHandleT = TypeVar("OpaqueHandleT")


# ---------------------------------------------------------------------------
# Runtime validator input (concrete type).
#
# Validators at the framework boundary (agent_id, event_type, event data)
# accept any JSON scalar / container. Tuples, sets, custom objects are
# rejected by construction: they cannot cross the JSON wire. The Union
# keeps the type honest without resorting to ``Any`` or ``object``.
# ---------------------------------------------------------------------------

ValidatorInput = Union[
    str,
    int,
    float,
    bool,
    None,
    dict[str, "ValidatorInput"],
    list["ValidatorInput"],
]


# ---------------------------------------------------------------------------
# FastAPI transport surface (Protocol-only, no fastapi import).
#
# The intent-router installers accept the FastAPI primitives as
# arguments so the installer module itself stays
# ``import kntgraph.api``-able without FastAPI installed. The
# Protocols below give the installers static types without forcing a
# top-level ``from fastapi import ...``.
# ---------------------------------------------------------------------------


class RouterApp(Protocol):
    """The subset of FastAPI's ``app`` that the installers use.

    The ``**kwargs`` signature is widened to ``object`` (the
    framework's opaque boundary type) so the installers
    can pass Pydantic models, dicts, lists, status codes,
    and arbitrary HTTP responses without the type checker
    narrowing each call to the ``ValidatorInput`` union.
    The real validation happens at FastAPI's
    registration time.
    """

    def get(self, path: str, **kwargs: "object") -> Callable[..., "object"]: ...

    def post(self, path: str, **kwargs: "object") -> Callable[..., "object"]: ...

    def add_middleware(
        self, middleware_class: type, **kwargs: ValidatorInput
    ) -> None: ...


class Dependable(Protocol):
    """Subset of ``fastapi.Depends`` we need at the boundary."""

    def __call__(self, dependency: "object") -> object: ...


class HeaderParam(Protocol):
    """Subset of ``fastapi.Header`` we need at the boundary.

    The return type is widened to ``str | None`` (the
    shape the routers use) so the ``idempotency_key:
    Optional[str] = Header(default=None, ...)`` calls
    type-check without an explicit cast. The runtime
    default is supplied by FastAPI; the type
    annotation only exists to keep the static checker
    honest.
    """

    def __call__(
        self,
        default: "object" = ...,
        *,
        alias: str | None = None,
    ) -> "str | None": ...


class RouteDecorator(Protocol):
    """Subset of ``fastapi.routing.Route`` we need at the boundary.

    The full ``@app.get("/path", response_model=X, status_code=Y,
    responses={...})`` decorator accepts a wide range of
    arguments; declaring a single Protocol with ``**kwargs``
    (or a wide type) keeps the type checker from narrowing
    each call to the union ``ValidatorInput`` (the type used
    by the rest of the framework's "opaque" boundary).
    """

    def __call__(self, path: str, **kwargs: "object") -> "object": ...


class HTTPExceptionLike(Exception):
    """Exception with ``status_code`` and ``detail`` attributes.

    Matches ``fastapi.HTTPException`` structurally. Used as a Protocol
    (the installers raise it; FastAPI's middleware translates it to a
    response).
    """

    status_code: int
    detail: str

    def __init__(self, status_code: int, detail: str = "") -> None: ...


__all__ = [
    "ComponentT",
    "Dependable",
    "HeaderParam",
    "HTTPExceptionLike",
    "JsonScalar",
    "JsonValue",
    "KeyHandleT",
    "OpaqueHandleT",
    "RouterApp",
    "ValidatorInput",
    "ValidatorInputT",
]
