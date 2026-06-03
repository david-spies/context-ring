"""
Pytest configuration and shared fixtures.
"""

import os
import sys

import pytest

# Ensure src is importable from tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Default env vars for test suite
os.environ.setdefault("VNODE_REPLICAS", "50")
os.environ.setdefault("CONTEXT_RING_API_KEY", "test-secret-key")
os.environ.setdefault("INITIAL_AGENT_NODES", "")
os.environ.setdefault("LOG_LEVEL", "WARNING")
