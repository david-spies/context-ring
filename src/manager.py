"""
context_ring.manager
~~~~~~~~~~~~~~~~~~~~
Thread-safe, async-first wrapper around :py:class:`ConsistentHashRing`.

Responsibilities
----------------
* Owns the ``asyncio.Lock`` that guards ring mutations.
* Synchronises node membership to Redis using a gossip-compatible
  pattern (SADD / SREM on a shared set + pub/sub notifications).
* Provides helpers consumed by the FastAPI application layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Dict, List, Optional

import redis.asyncio as aioredis

from .ring import ConsistentHashRing, NodeInfo, RouteResult

logger = logging.getLogger("context_ring.manager")

REDIS_NODES_KEY = "context_ring:nodes"
REDIS_PUBSUB_CHANNEL = "context_ring:events"
REDIS_SESSION_PREFIX = "context_ring:session:"
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))


class RingManager:
    """
    Async facade over :py:class:`ConsistentHashRing` with optional
    Redis-backed state synchronisation for multi-proxy deployments.

    Usage
    -----
    .. code-block:: python

        manager = RingManager(replicas=128)
        await manager.startup()
        ...
        await manager.shutdown()
    """

    def __init__(
        self,
        replicas: int = 128,
        redis_url: Optional[str] = None,
    ) -> None:
        self._ring = ConsistentHashRing(replicas=replicas)
        self._lock = asyncio.Lock()
        self._redis_url = redis_url or os.getenv("REDIS_URL")
        self._redis: Optional[aioredis.Redis] = None
        self._pubsub_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self, initial_nodes: Optional[List[str]] = None) -> None:
        """Connect to Redis (if configured) and seed initial nodes."""
        if self._redis_url:
            try:
                self._redis = aioredis.from_url(
                    self._redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=3,
                )
                await self._redis.ping()
                logger.info("Redis connected: %s", self._redis_url)
                await self._sync_from_redis()
                self._pubsub_task = asyncio.create_task(self._pubsub_listener())
            except Exception as exc:
                logger.warning("Redis unavailable (%s) — running standalone.", exc)
                self._redis = None

        for node in (initial_nodes or []):
            node = node.strip()
            if node:
                await self.add_node(node, _broadcast=False)

    async def shutdown(self) -> None:
        """Cancel background tasks and close Redis connection."""
        if self._pubsub_task and not self._pubsub_task.done():
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass
        if self._redis:
            await self._redis.aclose()
            logger.info("Redis connection closed.")

    # ------------------------------------------------------------------
    # Node management
    # ------------------------------------------------------------------

    async def add_node(self, url: str, _broadcast: bool = True) -> NodeInfo:
        """Register a new agent node (thread-safe)."""
        async with self._lock:
            if url in self._ring.nodes:
                return self._ring.node_info(url)  # type: ignore[return-value]
            info = self._ring.add_node(url)

        if self._redis and _broadcast:
            await self._redis.sadd(REDIS_NODES_KEY, url)
            await self._publish("add", url)

        return info

    async def remove_node(self, url: str, _broadcast: bool = True) -> NodeInfo:
        """Evict an agent node; orphaned sessions will re-route on next request."""
        async with self._lock:
            info = self._ring.remove_node(url)

        if self._redis and _broadcast:
            await self._redis.srem(REDIS_NODES_KEY, url)
            await self._publish("remove", url)

        return info

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def route(self, session_id: str) -> Optional[RouteResult]:
        """
        Resolve *session_id* to an agent node URL.

        Also persists the session→node mapping to Redis so that other
        proxy instances can serve session-aware health checks.
        """
        result = self._ring.get_node(session_id)
        if result is None:
            return None

        # Persist session→node binding (fire-and-forget; don't block routing)
        if self._redis:
            asyncio.ensure_future(self._persist_session(session_id, result.node))

        return result

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def node_count(self) -> int:
        return self._ring.node_count

    @property
    def vnode_count(self) -> int:
        return self._ring.vnode_count

    @property
    def nodes(self):
        return self._ring.nodes

    def all_node_info(self) -> Dict[str, NodeInfo]:
        return self._ring.all_node_info()

    def arc_distribution(self) -> Dict[str, float]:
        return self._ring.arc_distribution()

    # ------------------------------------------------------------------
    # Redis helpers
    # ------------------------------------------------------------------

    async def _sync_from_redis(self) -> None:
        """Pull existing node membership from Redis on startup."""
        if not self._redis:
            return
        members = await self._redis.smembers(REDIS_NODES_KEY)
        for url in members:
            if url not in self._ring.nodes:
                self._ring.add_node(url)
        if members:
            logger.info("Synced %d nodes from Redis.", len(members))

    async def _persist_session(self, session_id: str, node_url: str) -> None:
        try:
            key = f"{REDIS_SESSION_PREFIX}{session_id}"
            await self._redis.set(key, node_url, ex=SESSION_TTL_SECONDS)
        except Exception as exc:
            logger.debug("Session persist failed: %s", exc)

    async def _publish(self, event: str, url: str) -> None:
        try:
            payload = json.dumps({"event": event, "node": url})
            await self._redis.publish(REDIS_PUBSUB_CHANNEL, payload)
        except Exception as exc:
            logger.debug("Publish failed: %s", exc)

    async def _pubsub_listener(self) -> None:
        """
        Subscribe to ring-change events from peer proxy instances.
        This implements the gossip-like synchronisation pattern.
        """
        if not self._redis:
            return
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(REDIS_PUBSUB_CHANNEL)
        logger.info("Subscribed to Redis pub/sub channel: %s", REDIS_PUBSUB_CHANNEL)
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    event, url = data.get("event"), data.get("node")
                    if event == "add" and url not in self._ring.nodes:
                        async with self._lock:
                            self._ring.add_node(url)
                        logger.info("GOSSIP add node=%s", url)
                    elif event == "remove" and url in self._ring.nodes:
                        async with self._lock:
                            self._ring.remove_node(url)
                        logger.info("GOSSIP remove node=%s", url)
                except Exception as exc:
                    logger.warning("Malformed pub/sub message: %s", exc)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(REDIS_PUBSUB_CHANNEL)
