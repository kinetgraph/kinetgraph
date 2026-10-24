# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from contextlib import asynccontextmanager

import uvicorn

from kntgraph.api import create_app
from kntgraph.api.auth import APIKeyVerifier, AuthError
from kntgraph.core.result import Err, Ok
from kntgraph.infra.config import Settings
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._factory import create_tool_router, create_worker_manager
from kntgraph.infra.redis._pool import create_redis_pool
from kntgraph.security import Principal, Role
from kntgraph.stream.event_log import EventLog
from weather_platform.contexts.weather.dispatcher import (
    build_weather_dispatcher,
    get_weather_tools,
)


class StaticAPIKeyVerifier(APIKeyVerifier):
    async def verify(self, api_key: str):
        if not api_key or api_key != "demo-key":
            return Err(AuthError("forbidden", "invalid key"))
        return Ok(
            Principal(
                agent_id="user-123",
                role=Role.agent,
                tenant_id="user-123",
                key_id="demo",
            )
        )


# 1. Global variables for background components
worker_manager = None
weather_dispatcher = None
# The process-wide Redis connection pool (ADR-084). Created in
# ``build_monolith``, closed in ``lifespan``.
redis_pool = None


@asynccontextmanager
async def lifespan(app):
    global worker_manager
    global weather_dispatcher
    global redis_pool
    print("\n[Lifespan] Starting Background Dispatchers and Workers...")

    # Start both components and AWAIT their ``start()`` so any
    # initialization error (e.g. ``ProcessPoolExecutor`` failing
    # to spawn, Redis refusing ``xgroup_create``) propagates to
    # the lifespan and surfaces in the uvicorn startup log. The
    # previous fire-and-forget ``asyncio.create_task`` wrapped
    # any startup exception inside an orphan task; the process
    # would stay up with no events flowing and no error in the
    # log.
    await worker_manager.start()
    await weather_dispatcher.start()

    yield

    print("\n[Lifespan] Stopping Background components...")
    await weather_dispatcher.stop()
    await worker_manager.stop()
    # Close the connection pool this process owns (ADR-084).
    await redis_pool.aclose()


def build_monolith():
    global worker_manager
    global weather_dispatcher
    global redis_pool

    # One connection pool for the whole process (ADR-084). The
    # framework will not build a pool the caller cannot close,
    # so the pool is created here and its client is shared by
    # every component below. The pool is closed in ``lifespan``.
    settings = Settings(redis_url="redis://:redispassword@localhost:6379")
    redis_pool = create_redis_pool(settings)
    redis = redis_pool.client
    log = EventLog(RedisEventLogAdapter(client=redis))

    tool_router = create_tool_router(settings=settings, client=redis)
    worker_manager = create_worker_manager(
        settings=settings, redis=redis, event_log=log
    )

    # Initialize Context Dispatchers
    weather_dispatcher = build_weather_dispatcher(log, redis, tool_router)

    # Register Tools to Worker Manager
    for tool in get_weather_tools():
        worker_manager.register(type(tool))

    # Configure API Gateway (Intent Router).
    # Tools register exclusively via ``worker_manager``
    # above (ADR-066 §4.4: the legacy ``ToolRegistry``
    # was removed in v0.18).
    verifier = StaticAPIKeyVerifier()

    app = create_app(log=log, worker_manager=worker_manager, verifier=verifier)
    app.router.lifespan_context = lifespan
    return app


app = build_monolith()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
