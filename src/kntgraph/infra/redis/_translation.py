# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Exception translation table at the storage layer.

Per ADR-077 §2.3: every Redis adapter in this package
catches a *narrow* set of third-party exceptions and
translates them to a typed ``RedisAdapterError``
subclass (``MemoryError``, ``SolutionStoreError``,
``IdempotencyConflict``, etc.). Before this module,
the same ``except (redis_exceptions.RedisError,
ConnectionError, TimeoutError, OSError) as exc: ...``
boilerplate was duplicated in 20+ methods across
``_auth/_redis.py``, ``_memory/_*.py``, ``_solution.py``
and the rest. The catch list lived in 20 different
files; changing it (e.g. adding a new exception
category when ``redis-py`` releases) was a 20-file
diff.

This module consolidates the translation table in one
place. The two public helpers are:

- :func:`translate_redis_call` — the main one. Catches
  the third-party exception, logs the failure with
  a structured payload, and returns
  ``Result[T, E]`` where ``E`` is the concrete error
  type the call site wants (``MemoryError``,
  ``SolutionStoreError``, ``IdempotencyConflict``,
  etc.). All the storage ``Result[..., MemoryError]``
  methods use this.

- :func:`translate_redis_call_fall_back` — for the
  read-side ``fail-open`` shape (ADR-049 §2.1.3). The
  Solution store's ``find_match`` and ``read_all``
  fall back to ``None`` / ``{}`` on Redis failure
  instead of returning ``Err``. This helper does the
  same translation but returns ``T | D`` instead of
  ``Result``.

Both helpers guarantee ADR-077 §2.5: ``asyncio.
CancelledError`` (and ``KeyboardInterrupt``,
``SystemExit``) is **not** caught. It inherits from
``BaseException``, not ``Exception``, so the standard
``except`` clause does not see it; cooperative
cancellation propagates to the operator's
shutdown signal unchanged.

Why a separate module and not part of ``_errors.py``
------------------------------------------------------------

``_errors.py`` holds the *exception hierarchy*. This
module holds the *translation function* that produces
those exceptions. The two are conceptually different
(declarations vs. behaviour) and the import graph is
cleaner with a small split. ADR-077 §3.3 considered
renaming ``_errors.py`` to ``_protocol.py`` to
publicise the hierarchy; that decision is deferred to
a future major-version migration (the rename touches
~30 import sites for no behaviour change).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import TypeVar

import structlog
from redis import exceptions as redis_exceptions

from ...core.result import Err, Ok, Result
from ._errors import RedisAdapterError

logger = structlog.get_logger()

T = TypeVar("T")
D = TypeVar("D")
E = TypeVar("E", bound=RedisAdapterError)


def _build_typed_error[E: RedisAdapterError](
    exc: BaseException,
    error_cls: type[E],
    *,
    key: str | None = None,
    **kwargs: object,
) -> E:
    """
    Construct a concrete ``RedisAdapterError`` subclass
    (``MemoryError``, ``SolutionStoreError``,
    ``IdempotencyConflict``, ...) from the third-party
    exception.

    The constructor signature varies per subclass —
    ``MemoryError(*, key=...)``,
    ``SolutionStoreError(*, tool_name=..., params_fingerprint=...)``,
    ``IdempotencyConflict(idem_key)`` — so the helper
    inspects the constructor signature and forwards
    only the kwargs it accepts. ``**kwargs`` that the
    constructor does not name are dropped (they are
    already in the structured log payload, so the
    MemoryError instance does not need to carry them).
    """
    msg = f"redis error: {exc}"
    sig = inspect.signature(error_cls.__init__)
    accepted: dict[str, object] = {}
    # ``key`` is special: ``MemoryError`` defaults to
    # ``key=None``; passing it explicitly is meaningful
    # only when the caller provides a non-None value.
    if key is not None and "key" in sig.parameters:
        accepted["key"] = key
    # Forward only kwargs the constructor accepts.
    # ``None`` values are skipped (they carry no
    # information; the default in the constructor is
    # the same as "not passed").
    for name, value in kwargs.items():
        if value is None:
            continue
        if name in sig.parameters:
            accepted[name] = value
    return error_cls(msg, **accepted)


async def translate_redis_call[T, E: RedisAdapterError](
    op: Awaitable[T],
    *,
    op_name: str,
    error_cls: type[E],
    key: str | None = None,
    **log_ctx: object,
) -> Result[T, E]:
    """
    Run ``await op``; on the third-party Redis exception
    surface, log and return ``Err(error_cls(...))``; on
    success, return ``Ok(value)``.

    Per ADR-077 §2.3: the catch list is
    ``(redis_exceptions.RedisError, ConnectionError,
    TimeoutError, OSError)`` — narrow, exhaustive of
    the third-party transport failures the framework
    needs to translate. ``asyncio.CancelledError`` is
    **not** caught (it inherits from ``BaseException``,
    not ``Exception``); cooperative cancellation
    propagates unchanged.

    Parameters
    ----------
    op:
        The Redis operation to await. Typically the
        bare ``self.client.<method>(...)`` call without
        an ``await``; the helper awaits it.
    op_name:
        The structlog event-name suffix. The full event
        is ``f"{op_name}.redis_error"`` (a single
        dotted token for operator-side dashboard
        filters; matches every existing site in this
        package).
    error_cls:
        The concrete ``RedisAdapterError`` subclass to
        construct on failure. ``MemoryError`` for the
        memory tiers, ``SolutionStoreError`` for the
        Solution tier, ``IdempotencyConflict`` for the
        EventLog idempotency window. The helper builds
        the instance with the conventional
        ``f"redis error: {exc}"`` message and the
        caller-supplied ``key``.
    key:
        The Redis key that failed (for the structured
        log payload and the ``MemoryError.key``
        attribute). ``None`` is accepted for
        transport-level operations that do not have a
        key (``SCAN``-family operations, ``XINFO``,
        etc.).
    **log_ctx:
        Extra structured fields appended to the log
        payload. Used by call sites that need to
        include the request-specific identity
        (e.g. ``tool_name=``, ``digest=``,
        ``params_fingerprint=``).

    Returns
    -------
    ``Result[T, E]``. ``Ok(value)`` on success;
    ``Err(error_cls(...))`` on the catch list.
    """
    try:
        return Ok(await op)
    except (redis_exceptions.RedisError, ConnectionError, TimeoutError, OSError) as exc:
        logger.warning(
            f"{op_name}.redis_error",
            key=key,
            error=str(exc),
            **log_ctx,
        )
        return Err(_build_typed_error(exc, error_cls, key=key, **log_ctx))


async def translate_redis_call_fall_back[T, D](
    op: Awaitable[T],
    *,
    op_name: str,
    fallback: D,
    key: str | None = None,
    **log_ctx: object,
) -> T | D:
    """
    Run ``await op``; on third-party Redis failure,
    log and return ``fallback``; on success, return
    the awaited value.

    This is the **fail-open** variant of
    :func:`translate_redis_call`. The Solution store's
    read-side methods (``find_match``, ``read_all``)
    use this: a Redis transport failure does not
    surface as an error to the caller; the read-side
    is best-effort and the dispatcher falls back to
    the LLM path. Per ADR-049 §2.1.3.

    The structured log payload is identical to
    :func:`translate_redis_call`'s (the dashboard
    sees ``solution_store.find_match.redis_error``,
    etc.); the only difference is the return shape.

    Parameters
    ----------
    op:
        The Redis operation to await.
    op_name:
        Structlog event-name suffix.
    fallback:
        The value to return on Redis failure. The
        type parameter ``D`` is the caller's
        declared fallback type (``None``,
        ``dict[str, CachedSolution]``, etc.).
    key:
        Optional Redis key for the log payload.
    **log_ctx:
        Extra structured fields (see
        :func:`translate_redis_call`).

    Returns
    -------
    ``T | D``. ``await op``'s value on success;
    ``fallback`` on the catch list.
    """
    try:
        return await op
    except (redis_exceptions.RedisError, ConnectionError, TimeoutError, OSError) as exc:
        logger.warning(
            f"{op_name}.redis_error",
            key=key,
            error=str(exc),
            **log_ctx,
        )
        return fallback


__all__ = [
    "translate_redis_call",
    "translate_redis_call_fall_back",
]
