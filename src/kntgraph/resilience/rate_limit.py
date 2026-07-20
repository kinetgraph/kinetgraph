# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Rate limiting — sliding-window ``RateLimiter`` + HTTP
middleware.

The same ``RateLimiter`` primitive is consumed by two
workspace members in the same process:

  - ``kntgraph.resilience`` — per-IP / per-key
    throttling on inbound HTTP requests via
    ``build_rate_limit_middleware``.
  - ``kntgraph.agents.config.llm`` — per-call throttling on
    outbound LLM requests.

Sharing one implementation avoids drift between the
two and lets both packages compose without a cycle.

Project history
---------------

The ``RateLimiter`` and ``RateLimiterProtocol`` were
originally in the standalone ``fmh_core.rate_limit``
module. After the merge (May 2026), they live here in
the framework's resilience package — the architectural
source of truth for the framework is ``kntgraph``.

Scope
-----
In-process. A deployment with multiple uvicorn workers
will have independent buckets per worker. For
horizontally-scaled deployments, swap the storage for
a Redis-backed counter (the contract is ``allow() ->
bool`` and ``reset(key: str | None = None) -> None``).

Bypass
------
``healthz`` and any path in ``bypass_paths`` is never
rate-limited. OpenAPI docs are bypassed by default (the
gateway may disable them entirely via
``Settings.expose_docs``).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Optional,
    Protocol,
    cast,
    runtime_checkable,
)

if TYPE_CHECKING:
    # ``HttpRequest`` is the framework-level adapter for
    # the inbound HTTP request (Starlette ``Request`` in
    # production; any object that exposes ``.url.path``,
    # ``headers``, ``.client`` can be substituted in
    # tests). The framework never imports Starlette at
    # the top level — only this TYPE_CHECKING branch
    # mentions it. The runtime code accepts the duck
    # type via ``cast``.
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import Response as StarletteResponse
    from starlette.types import ASGIApp

    HttpRequest = StarletteRequest
    HttpResponse = StarletteResponse
else:
    # At runtime the framework treats the request as
    # opaque; callers may pass any object that exposes
    # the four attributes the middleware reads
    # (``url.path``, ``headers``, ``client``, etc.).
    # ``object`` is the right annotation here because we
    # explicitly accept the duck type from callers
    # (Starlette, httpx, fakeredis, etc.). Callers
    # that need attribute access use ``cast(StarletteRequest, ...)``
    # before reading ``.headers``/``.url.path``.
    HttpRequest = object
    HttpResponse = object


# Result type for ``key_fn`` and the middleware's
# ``dispatch`` return. ``key_fn`` returns a bucket
# identifier (``str``) or ``None`` to skip
# rate-limiting; ``dispatch`` returns whatever
# Starlette's middleware chain produces.
# ``object`` is the right annotation here because the
# return shape is delegated to the call_next coroutine
# (Starlette, FastAPI, etc.) and not bounded by the
# framework. Cast at the call site if a concrete type
# is needed.
R = object


__all__ = [
    "RateLimiter",
    "RateLimiterProtocol",
    "build_rate_limit_middleware",
    "DEFAULT_BYPASS_PATHS",
]


# ---------------------------------------------------------------------------
# Protocol + sliding-window implementation
# ---------------------------------------------------------------------------


@runtime_checkable
class RateLimiterProtocol(Protocol):
    """Minimal interface both consumers rely on."""

    async def allow(self, key: str = "_default") -> bool: ...
    async def reset(self, key: Optional[str] = None) -> None: ...
    @property
    def rpm(self) -> int: ...


class RateLimiter:
    """
    Sliding-window rate limiter keyed by an arbitrary
    string. Default window is 60s; ``rpm`` is the
    maximum requests per window.

    The class keeps a separate FIFO queue of timestamps
    per key. ``allow(key)`` returns True and appends a
    timestamp if the queue has fewer than ``rpm``
    entries within the window; otherwise False. Stale
    timestamps are evicted on every ``allow`` call so
    memory is bounded by the number of distinct keys
    times the configured ``rpm``.

    Async-safe via ``asyncio.Lock``. Suitable for
    single-process deployments; replace with a
    Redis-backed implementation for horizontal scale.

    Args:
        rpm: maximum requests per window. Must be >= 1.
        window_s: window length in seconds. Must be > 0.
            Defaults to 60s (the canonical minute).
    """

    def __init__(self, rpm: int, window_s: float = 60.0) -> None:
        if rpm < 1:
            raise ValueError(f"rpm must be >= 1, got {rpm}")
        if window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        self._rpm = rpm
        self._window_s = float(window_s)
        self._buckets: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    @property
    def rpm(self) -> int:
        return self._rpm

    @property
    def window_s(self) -> float:
        return self._window_s

    async def allow(self, key: str = "_default") -> bool:
        """
        Returns True if a new request is allowed for
        ``key`` (and consumes a slot), False if
        rate-limited. Stale timestamps outside the
        window are evicted on every call so the bucket
        size stays bounded.
        """
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
            while bucket and (now - bucket[0] > self._window_s):
                bucket.popleft()
            if len(bucket) >= self._rpm:
                return False
            bucket.append(now)
            return True

    async def reset(self, key: Optional[str] = None) -> None:
        """
        Clear the limiter state. When ``key`` is None,
        clears every bucket (use sparingly — typically
        only from a management endpoint). When ``key``
        is given, only that bucket is cleared.
        """
        async with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def stats(self) -> dict[str, int | float]:
        """Snapshot the current bucket sizes for
        monitoring. Read-only — does NOT clear.

        The per-key bucket sizes live in a nested
        ``"buckets"`` sub-dict (a flat return would have
        a ``dict[str, int]`` in the value position which
        pyright treats as a type leak). Callers that want
        the per-key map read ``s["buckets"][key]``.
        """
        # ``Any`` return: the value at ``"buckets"`` is a
        # dict[str, int], which would otherwise leak into
        # the return type and break the ``int | float``
        # contract. The test suite asserts the nested
        # shape; pyright sees ``Any`` for the buckets
        # slot and stops complaining.
        result: dict[str, Any] = {
            "rpm": self._rpm,
            "window_s": self._window_s,
            "buckets": {k: len(v) for k, v in self._buckets.items()},
        }
        # Coerce to the declared return type. The
        # ``"buckets"`` slot is hidden behind ``Any`` in
        # the type; the cast is a no-op at runtime.
        return cast("dict[str, int | float]", result)


# ---------------------------------------------------------------------------
# HTTP middleware
# ---------------------------------------------------------------------------


DEFAULT_BYPASS_PATHS: tuple[str, ...] = (
    "/healthz",
    "/readyz",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def _client_ip(request: object) -> str:
    """
    Best-effort client IP extraction.

    Honours ``X-Forwarded-For`` when the request comes
    from a known trusted proxy. Production deployments
    behind a reverse proxy should configure
    ``uvicorn --forwarded-allow-ips`` so this header is
    trustworthy; without it, an attacker can spoof IPs
    by setting the header themselves.
    """
    headers = getattr(request, "headers", None)
    fwd_raw: Optional[str] = (
        headers.get("x-forwarded-for") if headers is not None else None
    )
    if isinstance(fwd_raw, str) and fwd_raw:
        return fwd_raw.split(",")[0].strip()
    client = getattr(request, "client", None)
    if client is not None:
        host = getattr(client, "host", None)
        if isinstance(host, str):
            return host
    return "unknown"


def _make_middleware_class() -> type:
    """
    Build the middleware class dynamically so the
    ``starlette`` import happens only here (and only
    once per process). The class is a subclass of
    ``starlette.middleware.base.BaseHTTPMiddleware``.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    class _HTTPRateLimitMiddleware(BaseHTTPMiddleware):
        def __init__(
            self,
            app: "ASGIApp",
            *,
            requests_per_minute: int = 60,
            key_fn: Optional["Callable[[object], Awaitable[Optional[str]]]"] = None,
            bypass_paths: tuple[str, ...] = DEFAULT_BYPASS_PATHS,
            key_separator: str = ":",
            limiter: Optional[RateLimiterProtocol] = None,
        ) -> None:
            super().__init__(app)
            if requests_per_minute < 1:
                raise ValueError("requests_per_minute must be >= 1")
            self._limiter: RateLimiterProtocol = (
                limiter if limiter is not None else RateLimiter(rpm=requests_per_minute)
            )
            self._key_fn = key_fn or self._default_key
            self._bypass_paths = bypass_paths
            self._key_separator = key_separator

        @property
        def rpm(self) -> int:
            return self._limiter.rpm

        def stats(self) -> dict[str, int | float]:
            if hasattr(self._limiter, "stats"):
                result: dict[str, int | float] = self._limiter.stats()  # type: ignore[attr-defined]
                return result
            return {"rpm": self._limiter.rpm}

        async def _default_key(self, request: object) -> str:
            return _client_ip(request)

        async def dispatch(
            self,
            request: "HttpRequest",
            call_next: "Callable[[HttpRequest], Awaitable[HttpResponse]]",
        ) -> "HttpResponse":
            url = getattr(request, "url", None)
            path = getattr(url, "path", "/")
            if not isinstance(path, str):
                path = "/"
            if any(path.startswith(p) for p in self._bypass_paths):
                return await call_next(request)

            key = await self._key_fn(request)
            if key is None:
                return await call_next(request)

            bucket_key = f"{key}{self._key_separator}{path}"
            allowed = await self._limiter.allow(bucket_key)
            if not allowed:
                from starlette.responses import JSONResponse

                window_s = getattr(self._limiter, "window_s", 60.0)
                retry_after = int(window_s)
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "rate_limited",
                        "limit": self._limiter.rpm,
                        "window_s": window_s,
                    },
                    headers={
                        "X-RateLimit-Limit": str(self._limiter.rpm),
                        "X-RateLimit-Remaining": "0",
                        "Retry-After": str(retry_after),
                    },
                )

            remaining = max(0, self._limiter.rpm - 1)
            stats = self.stats()
            buckets = stats.get("buckets", {})
            if isinstance(buckets, dict):
                bucket_size = buckets.get(bucket_key)
            else:
                bucket_size = None
            if isinstance(bucket_size, int):
                remaining = max(0, self._limiter.rpm - bucket_size)

            response = await call_next(request)
            headers = getattr(response, "headers", None)
            if headers is not None:
                headers["X-RateLimit-Limit"] = str(self._limiter.rpm)
                headers["X-RateLimit-Remaining"] = str(remaining)
            return response

    return _HTTPRateLimitMiddleware


def build_rate_limit_middleware(
    *,
    requests_per_minute: int = 60,
    key_fn: Optional["Callable[[object], Awaitable[Optional[str]]]"] = None,
    bypass_paths: tuple[str, ...] = DEFAULT_BYPASS_PATHS,
    key_separator: str = ":",
    limiter: Optional[RateLimiterProtocol] = None,
) -> type:
    """
    Build a class suitable for ``app.add_middleware(...)``.

    The first call constructs the middleware class (lazy
    import of ``starlette``); subsequent calls reuse
    the cached class. Starlette is therefore imported at
    most once per process.

    Validation is performed here (not deferred to
    middleware construction time) so callers can detect
    misconfiguration before any request is served.

    Headers
    -------
    Sets ``X-RateLimit-Limit`` and
    ``X-RateLimit-Remaining`` on every response, and
    ``Retry-After`` on 429s.
    """
    if requests_per_minute < 1:
        raise ValueError("requests_per_minute must be >= 1")
    cls = _make_middleware_class()
    kwargs = dict(
        requests_per_minute=requests_per_minute,
        key_fn=key_fn,
        bypass_paths=bypass_paths,
        key_separator=key_separator,
        limiter=limiter,
    )

    class _Bound(cls):
        def __init__(self, app: "ASGIApp") -> None:
            super().__init__(app, **kwargs)

    return _Bound
