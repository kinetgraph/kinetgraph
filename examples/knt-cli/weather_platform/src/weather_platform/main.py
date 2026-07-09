
import asyncio
import logging
import uvicorn
from contextlib import asynccontextmanager

from kntgraph.api import create_app
from kntgraph.api.auth import APIKeyVerifier, AuthError
from kntgraph.security import Principal, Role
from kntgraph.core.result import Ok, Err

class StaticAPIKeyVerifier(APIKeyVerifier):
    async def verify(self, api_key: str):
        if not api_key or api_key != "demo-key":
            return Err(AuthError("forbidden", "invalid key"))
        return Ok(Principal(agent_id="user-123", role=Role.agent, tenant_id="user-123", key_id="demo"))
from kntgraph.agents.tools.protocol import ToolRegistry
from kntgraph.stream.event_log import EventLog
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._pool import create_redis_pool
from kntgraph.infra.config import Settings
from kntgraph.tools.router import ToolRouter
from kntgraph.tools.manager import WorkerManager

from weather_platform.contexts.weather.dispatcher import build_weather_dispatcher, get_weather_tools

# 1. Global variables for background components
worker_manager = None
weather_dispatcher = None

@asynccontextmanager
async def lifespan(app):
    global worker_manager
    global weather_dispatcher
    print("\n[Lifespan] Starting Background Dispatchers and Workers...")
    
    # Start the event loops in the background
    worker_task = asyncio.create_task(worker_manager.start())
    weather_task = asyncio.create_task(weather_dispatcher.start())
    
    yield
    
    print("\n[Lifespan] Stopping Background components...")
    await weather_dispatcher.stop()
    await worker_manager.stop()

def build_monolith():
    global worker_manager
    global weather_dispatcher
    
    # Use framework connection pool
    settings = Settings(redis_url="redis://:redispassword@localhost:6379")
    redis = create_redis_pool(settings).client
    log = EventLog(RedisEventLogAdapter(client=redis))
    
    tool_router = ToolRouter(redis)
    worker_manager = WorkerManager(redis=redis, event_log=log)
    
    # Initialize Context Dispatchers
    weather_dispatcher = build_weather_dispatcher(log, redis, tool_router)
    
    # Register Tools to Worker Manager
    for tool in get_weather_tools():
        worker_manager.register(type(tool))
    
    # Configure API Gateway (Intent Router)
    registry = ToolRegistry()
    for tool in get_weather_tools():
        registry.register(tool)
        
    verifier = StaticAPIKeyVerifier()
    
    app = create_app(log=log, registry=registry, verifier=verifier)
    app.router.lifespan_context = lifespan
    return app

app = build_monolith()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

