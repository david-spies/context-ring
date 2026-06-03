"""
context_ring.ring
~~~~~~~~~~~~~~~~~
Enterprise-grade consistent hash ring with virtual nodes (vnodes) for
state-preserving AI agent session routing.

Algorithm:  MurmurHash3 (mmh3) over a sorted list with bisect for O(log N)
            clockwise-nearest-node lookups.
Thread safety: the ring is NOT thread-safe by itself; callers must acquire
            RingManager's asyncio.Lock before mutating state.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import mmh3

logger = logging.getLogger("context_ring.ring")


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RouteResult:
    """Returned by :py:meth:`ConsistentHashRing.get_node`."""
    node: str
    key_hash: int
    vnode_hash: int
    vnode_index: int


@dataclass
class NodeInfo:
    """Runtime metadata tracked per physical node."""
    url: str
    replicas: int
    active_sessions: int = 0
    total_routed: int = 0
    vnodes: List[int] = field(default_factory=list)  # sorted hashes for this node

    @property
    def load_factor(self) -> float:
        """Fraction of sessions on this node (0.0–1.0). Caller normalises."""
        return float(self.active_sessions)


# ---------------------------------------------------------------------------
# ConsistentHashRing
# ---------------------------------------------------------------------------

class ConsistentHashRing:
    """
    A consistent hash ring backed by a sorted list for binary-search lookups.

    Physical nodes are represented by *replicas* virtual nodes (vnodes)
    distributed uniformly around the ring, which dramatically reduces
    hotspot probability compared to single-node placement.

    Complexity
    ----------
    add_node    O(R log R)  R = replicas
    remove_node O(R log N)  N = total vnodes
    get_node    O(log N)
    """

    def __init__(self, replicas: int = 100) -> None:
        if replicas < 1:
            raise ValueError(f"replicas must be ≥ 1, got {replicas}")
        self.replicas: int = replicas
        self._ring: List[int] = []                 # sorted vnode hash positions
        self._vnode_map: Dict[int, str] = {}       # hash -> physical node URL
        self._vnode_idx_map: Dict[int, int] = {}   # hash -> vnode index
        self._nodes: Dict[str, NodeInfo] = {}

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(key: str) -> int:
        """32-bit unsigned MurmurHash3.  Non-cryptographic; extremely fast."""
        return mmh3.hash(key, signed=False)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add_node(self, url: str) -> NodeInfo:
        """
        Add *url* to the ring, placing *replicas* vnodes around it.

        Returns the :py:class:`NodeInfo` for the newly added node.
        Raises :py:exc:`ValueError` if *url* is already registered.
        """
        if url in self._nodes:
            raise ValueError(f"Node '{url}' is already registered.")

        info = NodeInfo(url=url, replicas=self.replicas)
        self._nodes[url] = info

        for i in range(self.replicas):
            vkey = f"{url}#vnode-{i}"
            h = self._hash(vkey)
            bisect.insort(self._ring, h)
            self._vnode_map[h] = url
            self._vnode_idx_map[h] = i
            info.vnodes.append(h)

        logger.info("MOUNT node=%s vnodes=%d total_ring_size=%d", url, self.replicas, len(self._ring))
        return info

    def remove_node(self, url: str) -> NodeInfo:
        """
        Gracefully remove *url* from the ring.

        Only the 1/N sessions mapped to this node will experience a
        cache miss on their next request.  Returns the removed
        :py:class:`NodeInfo`.
        Raises :py:exc:`KeyError` if *url* is not registered.
        """
        if url not in self._nodes:
            raise KeyError(f"Node '{url}' is not registered.")

        info = self._nodes.pop(url)

        for h in info.vnodes:
            idx = bisect.bisect_left(self._ring, h)
            if idx < len(self._ring) and self._ring[idx] == h:
                del self._ring[idx]
            self._vnode_map.pop(h, None)
            self._vnode_idx_map.pop(h, None)

        logger.info("EVICT node=%s sessions_orphaned=%d total_ring_size=%d",
                    url, info.active_sessions, len(self._ring))
        return info

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def get_node(self, session_id: str) -> Optional[RouteResult]:
        """
        Route *session_id* clockwise to its nearest vnode owner.

        Returns ``None`` if the ring is empty.
        Complexity: O(log N) where N = total vnode count.
        """
        if not self._ring:
            return None

        key_hash = self._hash(session_id)
        idx = bisect.bisect_right(self._ring, key_hash)
        if idx == len(self._ring):
            idx = 0                                # wrap around (ring semantics)

        vnode_hash = self._ring[idx]
        node_url = self._vnode_map[vnode_hash]
        vnode_idx = self._vnode_idx_map[vnode_hash]

        # Update routing counters
        node_info = self._nodes[node_url]
        node_info.total_routed += 1

        return RouteResult(
            node=node_url,
            key_hash=key_hash,
            vnode_hash=vnode_hash,
            vnode_index=vnode_idx,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def vnode_count(self) -> int:
        return len(self._ring)

    @property
    def nodes(self) -> Set[str]:
        return set(self._nodes.keys())

    def node_info(self, url: str) -> Optional[NodeInfo]:
        return self._nodes.get(url)

    def all_node_info(self) -> Dict[str, NodeInfo]:
        return dict(self._nodes)

    def arc_distribution(self) -> Dict[str, float]:
        """
        Return each node's share of the hash-space as a fraction [0.0, 1.0].
        Useful for load-balance health checks and dashboards.
        """
        if not self._ring or not self._nodes:
            return {}

        MAX_HASH = 0xFFFFFFFF
        arc_sizes: Dict[str, int] = {url: 0 for url in self._nodes}

        for i, h in enumerate(self._ring):
            prev_h = self._ring[i - 1] if i > 0 else 0
            arc = (MAX_HASH - prev_h + h) if i == 0 else (h - prev_h)
            arc_sizes[self._vnode_map[h]] += arc

        total = sum(arc_sizes.values()) or 1
        return {url: arc / total for url, arc in arc_sizes.items()}

    def __repr__(self) -> str:
        return (f"<ConsistentHashRing nodes={self.node_count} "
                f"vnodes={self.vnode_count} replicas={self.replicas}>")
