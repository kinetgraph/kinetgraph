# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tools worker - primitives for the Tool Worker Pattern (ADR-036).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, TypeVar, get_type_hints

from pydantic import create_model

T = TypeVar("T", bound=type)


def tool_worker(
    *,
    name: str,
    description: str = "",
    # ``max_concurrency`` is the per-tool execution concurrency.
    # Each pool worker is a separate process that loads the
    # tool's heavy dependencies (torch, docling, GLiNER) on first
    # use. A default of 10 let ten workers load the same model in
    # parallel and exceed the cgroup (post-mortem 2026-10-07, OOM
    # 137 on the backoffice's 4 GB cgroup). Scale horizontally
    # through the ECS ``desired_count`` instead of the local pool.
    max_concurrency: int = 1,
    retries: int = 3,
    idempotent: bool = True,
    timeout_seconds: float | None = None,
) -> Callable[[T], T]:
    """
    Marks a class as a Tool Worker (ADR-036).

    The class must expose an ``invoke`` method that accepts
    ``idempotency_key``. The decorator extracts the JSON schema of
    that method with Pydantic and injects ``name``, ``description``
    and ``input_schema``, so the class satisfies the ``Describable``
    protocol.

    ``idempotent`` declares whether re-invoking the tool is safe.
    ``True`` lets the framework re-run it during recovery; ``False``
    marks a tool with side effects that must happen once (a PIX, a
    bank transfer, a file mutation outside a transaction). The reaper
    resolves a non-idempotent request with a failure instead of
    re-executing it.

    ``timeout_seconds`` is the tool's own latency budget. ``None``
    means "no opinion" — the framework applies its default. A
    declared value travels with the request, so the reaper reclaims
    the request on the tool's threshold rather than the global
    ``reaper_idle_time``.
    """

    def decorator(cls: T) -> T:
        if not hasattr(cls, "invoke") or not callable(cls.invoke):
            raise TypeError(f"Tool {cls.__name__} must implement an 'invoke' method.")

        invoke_method = cls.invoke
        sig = inspect.signature(invoke_method)

        # Validate idempotency_key
        if "idempotency_key" not in sig.parameters:
            raise TypeError(
                f"Tool {cls.__name__}.invoke must accept 'idempotency_key' "
                "as a keyword-only argument."
            )
        idemp_param = sig.parameters["idempotency_key"]
        if idemp_param.kind not in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise TypeError(
                f"Tool {cls.__name__}.invoke parameter 'idempotency_key' "
                "must be passable as keyword."
            )

        # ``model_fields`` holds ``(type, FieldInfo)`` and
        # ``(type, default)`` tuples mixed with Pydantic's own
        # kwargs; the strict signature does not narrow that union.
        model_fields: dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
            if param_name in ("self", "idempotency_key"):
                continue

            param_type: Any = (
                Any if param.annotation is inspect.Parameter.empty else param.annotation
            )
            # ``from __future__ import annotations`` stores the
            # annotation as a string, which Pydantic cannot resolve
            # on its own. The namespace of the class's own module
            # supplies the typing primitives it refers to.
            if isinstance(param_type, str):
                try:
                    import importlib as _importlib
                    import typing as _typing

                    mod = _importlib.import_module(cls.__module__)
                    ns = {**vars(_typing), **vars(mod)}
                    hints = get_type_hints(invoke_method, globalns=ns, localns=ns)
                    param_type = hints.get(param_name, Any)
                except (
                    NameError,
                    TypeError,
                    AttributeError,
                    ImportError,
                    KeyError,
                    ValueError,
                ):
                    param_type = Any

            # A parameter without a default is required; the
            # sentinel ``...`` marks it as such in the model.
            model_fields[param_name] = (
                param_type,
                ... if param.default is inspect.Parameter.empty else param.default,
            )

        InputModel = create_model(
            f"{cls.__name__}Input", __module__=cls.__module__, **model_fields
        )
        schema = InputModel.model_json_schema()
        # The title repeats the model name, which the caller
        # already knows; a pure JSON schema carries no title.
        if "title" in schema:
            del schema["title"]

        cls.name = name
        cls.description = description
        cls.input_schema = schema
        cls.__tool_worker_max_concurrency__ = max_concurrency
        cls.__tool_worker_retries__ = retries
        # ADR-085 B': the reaper holds the tool **class** while the
        # ``Tool`` Protocol's ``idempotent`` flag is an instance
        # attribute. The decorator bridges the two.
        cls.__tool_worker_idempotent__ = idempotent
        # ADR-085: the tool's own latency budget.
        cls.__tool_worker_timeout_seconds__ = timeout_seconds

        return cls

    return decorator
