"""
context_ring
~~~~~~~~~~~~
State-preserving consistent-hash load balancer for AI agent swarms.
"""

__version__ = "1.0.0"
__author__ = "Context-Ring Contributors"

from .ring import ConsistentHashRing, NodeInfo, RouteResult
from .manager import RingManager

__all__ = [
    "ConsistentHashRing",
    "NodeInfo",
    "RouteResult",
    "RingManager",
]
