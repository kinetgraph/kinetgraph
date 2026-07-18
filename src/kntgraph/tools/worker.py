# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tools worker - primitives for the Tool Worker Pattern (ADR-036).
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, TypeVar, get_type_hints

from pydantic import create_model

T = TypeVar("T", bound=type)


def tool_worker(
    *,
    name: str,
    description: str = "",
    max_concurrency: int = 10,
    retries: int = 3,
) -> Callable[[T], T]:
    """
    Decorator to mark a class as a Tool Worker (ADR-036).

    Validates that the class has an `invoke` method taking `idempotency_key`,
    and automatically extracts the JSON schema from the method's signature
    using Pydantic. Injects `name`, `description`, and `input_schema` so
    the class satisfies the `Describable` protocol.
    """

    def decorator(cls: T) -> T:
        if not hasattr(cls, "invoke") or not callable(getattr(cls, "invoke")):
            raise TypeError(f"Tool {cls.__name__} must implement an 'invoke' method.")

        invoke_method = getattr(cls, "invoke")
        sig = inspect.signature(invoke_method)

        # Validate idempotency_key
        if "idempotency_key" not in sig.parameters:
            raise TypeError(
                f"Tool {cls.__name__}.invoke must accept 'idempotency_key' as a keyword-only argument."
            )
        idemp_param = sig.parameters["idempotency_key"]
        if idemp_param.kind not in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise TypeError(
                f"Tool {cls.__name__}.invoke parameter 'idempotency_key' must be passable as keyword."
            )

        # Build dynamic Pydantic model to extract schema
        # Skip 'self' and 'idempotency_key'. ``model_fields``
        # is typed as ``dict[str, Any]`` because Pydantic's
        # ``create_model`` accepts a heterogeneous mix of
        # ``(type, FieldInfo)`` and ``(type, default)`` tuples
        # plus its own ``__config__``/``__base__``/etc.
        # kwargs; the strict signature does not narrow that
        # union, hence ``Any``.
        model_fields: dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
            if param_name in ("self", "idempotency_key"):
                continue

            param_type: Any = (
                Any if param.annotation is inspect.Parameter.empty else param.annotation
            )
            # Resolve forward references (Python 3.12+ stores
            # annotations as strings when ``from __future__
            # import annotations`` is in effect; Pydantic
            # cannot resolve ``Any`` from the local string
            # namespace). We use the global namespace of
            # the module the class lives in (classes do
            # not have a ``__globals__`` attribute) so
            # ``Any`` (and other typing primitives imported
            # in the user module) resolve correctly.
            if isinstance(param_type, str):
                try:
                    import importlib as _importlib
                    import typing as _typing

                    mod = _importlib.import_module(cls.__module__)
                    ns = {**vars(_typing), **vars(mod)}
                    hints = get_type_hints(invoke_method, globalns=ns, localns=ns)
                    param_type = hints.get(param_name, Any)
                except Exception:
                    param_type = Any

            if param.default is inspect.Parameter.empty:
                # Required parameter
                model_fields[param_name] = (param_type, ...)
            else:
                # Optional parameter
                model_fields[param_name] = (param_type, param.default)

        # Create a dynamic Pydantic model
        InputModel = create_model(  # type: ignore[call-overload]
            f"{cls.__name__}Input", __module__=cls.__module__, **model_fields
        )

        # Get JSON schema
        schema = InputModel.model_json_schema()

        # Clean up some Pydantic artifacts to make it a pure JSON schema
        if "title" in schema:
            del schema["title"]

        # Inject metadata into the class
        setattr(cls, "name", name)
        setattr(cls, "description", description)
        setattr(cls, "input_schema", schema)
        setattr(cls, "__tool_worker_max_concurrency__", max_concurrency)
        setattr(cls, "__tool_worker_retries__", retries)

        return cls

    return decorator
