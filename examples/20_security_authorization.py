import asyncio
import redis.asyncio as aioredis

from kntgraph.core.event import Event, EventClass, correlation_middleware
from kntgraph.stream.event_log import EventLog
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.security.keys import InMemoryKeyRegistry, generate_keypair
from kntgraph.security.signing import sign_event
from kntgraph.security.principal import (
    Principal,
    Role,
    Action,
    Resource,
    DefaultPolicy,
    principal_ctx,
)

def _banner(msg: str) -> None:
    print("\n" + "=" * 72)
    print(msg)
    print("=" * 72)

async def main() -> None:
    _banner("20 — Zero-Trust L2: Authorization (RBAC)")

    correlation_middleware.start(metadata={"example": "20"})
    redis = aioredis.from_url("redis://:redispassword@localhost:6379", db=15)
    await redis.flushdb()

    try:
        policy = DefaultPolicy()
        
        _banner("Scenario 1: Agent Role Authorization")
        
        # Build principal for an agent belonging to tenant-A
        agent_principal = Principal.from_agent_id(
            "tenant-A.agent-1",
            role=Role.agent,
            key_id="key-001"
        )
        
        # Test 1: Agent trying to write an event to its own tenant
        res_own = Resource(kind="event", tenant_id="tenant-A.agent-1")
        allowed = policy.allows(principal=agent_principal, resource=res_own, action=Action.write)
        print(f"Agent writing to own tenant-A.agent-1: {'ALLOWED' if allowed else 'DENIED'}")
        
        # Test 2: Agent trying to write an event to another tenant
        res_other = Resource(kind="event", tenant_id="tenant-B")
        allowed = policy.allows(principal=agent_principal, resource=res_other, action=Action.write)
        print(f"Agent writing to tenant-B: {'ALLOWED' if allowed else 'DENIED'}")

        _banner("Scenario 2: Admin Role Authorization")
        
        # Build principal for an admin
        admin_principal = Principal(
            agent_id="admin-1",
            role=Role.admin,
            tenant_id=None,
            key_id="key-002"
        )
        
        # Test 3: Admin trying to write an event to tenant-B
        allowed = policy.allows(principal=admin_principal, resource=res_other, action=Action.write)
        print(f"Admin writing to tenant-B: {'ALLOWED' if allowed else 'DENIED'}")
        
        # Test 4: Admin performing administer action
        res_system = Resource(kind="tenant", tenant_id="tenant-C")
        allowed = policy.allows(principal=admin_principal, resource=res_system, action=Action.administer)
        print(f"Admin administering tenant-C: {'ALLOWED' if allowed else 'DENIED'}")

        _banner("Scenario 3: Binding Principal Context")
        
        # Set the context variable to propagate identity down to EventLog/ToolInvoker
        token = principal_ctx.set(agent_principal)
        
        current_principal = principal_ctx.get()
        print(f"Bound Context Principal: {current_principal.agent_id} (Role: {current_principal.role.value})")
        
        # Cleanup context
        principal_ctx.reset(token)

    finally:
        correlation_middleware.clear()
        await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())
