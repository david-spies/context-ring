"""
tests.test_manager
~~~~~~~~~~~~~~~~~~
Integration tests for RingManager.

These tests require a running Redis instance.
They are skipped automatically when Redis is unavailable.

To run:
    REDIS_URL=redis://localhost:6379/0 pytest tests/test_manager.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from manager import RingManager

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/15")  # DB 15 = test isolation


# ─── Skip marker ──────────────────────────────────────────────────────────────

def _redis_available() -> bool:
    try:
        import redis
        c = redis.from_url(REDIS_URL, socket_connect_timeout=1)
        c.ping()
        c.close()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="Redis not available — set REDIS_URL and start a Redis instance.",
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def manager():
    mgr = RingManager(replicas=50, redis_url=REDIS_URL)
    await mgr.startup()
    yield mgr
    # Teardown: remove all test nodes
    for url in list(mgr.nodes):
        try:
            await mgr.remove_node(url)
        except Exception:
            pass
    await mgr.shutdown()


# ─── Tests ────────────────────────────────────────────────────────────────────

@requires_redis
@pytest.mark.asyncio
async def test_add_and_route(manager):
    await manager.add_node("http://agent-1:8001")
    result = await manager.route("test-session-abc")
    assert result is not None
    assert result.node == "http://agent-1:8001"


@requires_redis
@pytest.mark.asyncio
async def test_routing_deterministic(manager):
    await manager.add_node("http://agent-a:8001")
    await manager.add_node("http://agent-b:8002")

    session = "stable-session-xyz"
    r1 = await manager.route(session)
    r2 = await manager.route(session)
    assert r1.node == r2.node
    assert r1.key_hash == r2.key_hash


@requires_redis
@pytest.mark.asyncio
async def test_remove_node(manager):
    await manager.add_node("http://agent-x:8001")
    await manager.add_node("http://agent-y:8002")
    await manager.remove_node("http://agent-x:8001")

    assert manager.node_count == 1
    result = await manager.route("any-session")
    assert result.node == "http://agent-y:8002"


@requires_redis
@pytest.mark.asyncio
async def test_arc_distribution(manager):
    for i in range(3):
        await manager.add_node(f"http://agent-{i}:8001")
    dist = manager.arc_distribution()
    total = sum(dist.values())
    assert abs(total - 1.0) < 1e-5


@requires_redis
@pytest.mark.asyncio
async def test_redis_session_persisted(manager):
    """Verify session→node mapping is written to Redis."""
    import redis.asyncio as aioredis
    await manager.add_node("http://agent-persist:8001")
    await manager.route("persist-session-001")

    # Give the fire-and-forget task a moment to complete
    await asyncio.sleep(0.1)

    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    val = await r.get("context_ring:session:persist-session-001")
    await r.aclose()
    assert val == "http://agent-persist:8001"


@requires_redis
@pytest.mark.asyncio
async def test_startup_syncs_from_redis(manager):
    """A second manager instance should pick up nodes from Redis."""
    await manager.add_node("http://shared-agent:8001")

    # Spin up a second manager pointing at the same Redis
    mgr2 = RingManager(replicas=50, redis_url=REDIS_URL)
    await mgr2.startup()

    assert "http://shared-agent:8001" in mgr2.nodes

    await mgr2.shutdown()
