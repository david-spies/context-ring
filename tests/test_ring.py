"""
tests.test_ring
~~~~~~~~~~~~~~~
Unit tests for ConsistentHashRing.

Coverage targets:
* Hash determinism
* Add / remove node lifecycle
* Clockwise routing correctness
* Virtual node distribution
* Arc fraction accounting
* Edge cases: empty ring, single node, duplicate add
"""

from __future__ import annotations

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ring import ConsistentHashRing, RouteResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ring():
    return ConsistentHashRing(replicas=50)


@pytest.fixture()
def populated_ring():
    r = ConsistentHashRing(replicas=50)
    r.add_node("http://agent-1:8001")
    r.add_node("http://agent-2:8002")
    r.add_node("http://agent-3:8003")
    return r


# ---------------------------------------------------------------------------
# Hash determinism
# ---------------------------------------------------------------------------

class TestHash:
    def test_same_input_same_hash(self, ring):
        h1 = ring._hash("session-abc-123")
        h2 = ring._hash("session-abc-123")
        assert h1 == h2

    def test_different_inputs_different_hashes(self, ring):
        assert ring._hash("session-A") != ring._hash("session-B")

    def test_hash_unsigned_32bit(self, ring):
        for key in ["a", "hello world", "session_xyz_99999"]:
            h = ring._hash(key)
            assert 0 <= h <= 0xFFFFFFFF, f"Hash out of 32-bit range for key={key!r}"


# ---------------------------------------------------------------------------
# Add / Remove nodes
# ---------------------------------------------------------------------------

class TestNodeLifecycle:
    def test_add_single_node(self, ring):
        info = ring.add_node("http://agent-1:8001")
        assert ring.node_count == 1
        assert ring.vnode_count == 50
        assert info.url == "http://agent-1:8001"
        assert info.replicas == 50

    def test_add_multiple_nodes(self, ring):
        ring.add_node("http://agent-1:8001")
        ring.add_node("http://agent-2:8002")
        assert ring.node_count == 2
        assert ring.vnode_count == 100

    def test_duplicate_add_raises(self, ring):
        ring.add_node("http://agent-1:8001")
        with pytest.raises(ValueError, match="already registered"):
            ring.add_node("http://agent-1:8001")

    def test_remove_node(self, populated_ring):
        populated_ring.remove_node("http://agent-2:8002")
        assert populated_ring.node_count == 2
        assert populated_ring.vnode_count == 100
        assert "http://agent-2:8002" not in populated_ring.nodes

    def test_remove_unknown_raises(self, ring):
        with pytest.raises(KeyError, match="not registered"):
            ring.remove_node("http://ghost:9999")

    def test_remove_cleans_vnode_map(self, populated_ring):
        url = "http://agent-1:8001"
        info = populated_ring.node_info(url)
        vnodes_before = set(info.vnodes)
        populated_ring.remove_node(url)
        for h in vnodes_before:
            assert h not in populated_ring._vnode_map

    def test_ring_sorted_after_add(self, ring):
        for i in range(10):
            ring.add_node(f"http://agent-{i}:800{i}")
        hashes = ring._ring
        assert hashes == sorted(hashes)

    def test_ring_sorted_after_remove(self, populated_ring):
        populated_ring.remove_node("http://agent-2:8002")
        hashes = populated_ring._ring
        assert hashes == sorted(hashes)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

class TestRouting:
    def test_get_node_returns_result(self, populated_ring):
        result = populated_ring.get_node("my-session-id")
        assert isinstance(result, RouteResult)
        assert result.node in populated_ring.nodes

    def test_empty_ring_returns_none(self, ring):
        assert ring.get_node("any-session") is None

    def test_deterministic_routing(self, populated_ring):
        session = "deterministic-session-xyz"
        r1 = populated_ring.get_node(session)
        r2 = populated_ring.get_node(session)
        assert r1.node == r2.node
        assert r1.key_hash == r2.key_hash

    def test_consistent_after_add(self, populated_ring):
        """Adding a node should not reroute most sessions."""
        sessions = [f"session-{i}" for i in range(200)]
        before = {s: populated_ring.get_node(s).node for s in sessions}

        populated_ring.add_node("http://agent-4:8004")

        after = {s: populated_ring.get_node(s).node for s in sessions}
        remapped = sum(1 for s in sessions if before[s] != after[s])
        # Expect roughly 1/4 = 25% remapped; allow up to 40%
        assert remapped / len(sessions) < 0.40, (
            f"Too many sessions remapped: {remapped}/{len(sessions)}"
        )

    def test_consistent_after_remove(self, populated_ring):
        """Removing a node should reroute only its share (~1/3)."""
        sessions = [f"session-{i}" for i in range(200)]
        before = {s: populated_ring.get_node(s).node for s in sessions}

        populated_ring.remove_node("http://agent-2:8002")

        after = {s: populated_ring.get_node(s).node for s in sessions}
        remapped = sum(1 for s in sessions if before[s] != after[s])
        assert remapped / len(sessions) < 0.50, (
            f"Too many sessions remapped: {remapped}/{len(sessions)}"
        )

    def test_single_node_all_route_to_it(self, ring):
        ring.add_node("http://only-agent:8001")
        for i in range(20):
            result = ring.get_node(f"session-{i}")
            assert result.node == "http://only-agent:8001"

    def test_routing_increments_total_routed(self, ring):
        ring.add_node("http://agent-1:8001")
        for _ in range(5):
            ring.get_node("session-X")
        info = ring.node_info("http://agent-1:8001")
        assert info.total_routed == 5


# ---------------------------------------------------------------------------
# Arc distribution
# ---------------------------------------------------------------------------

class TestArcDistribution:
    def test_arc_fractions_sum_to_one(self, populated_ring):
        dist = populated_ring.arc_distribution()
        total = sum(dist.values())
        assert abs(total - 1.0) < 1e-6

    def test_arc_all_nodes_present(self, populated_ring):
        dist = populated_ring.arc_distribution()
        assert set(dist.keys()) == populated_ring.nodes

    def test_arc_empty_ring(self, ring):
        assert ring.arc_distribution() == {}

    def test_arc_reasonable_distribution(self, populated_ring):
        """With 50 replicas across 3 nodes, no node should hold >80% of the ring.
        Hash distribution is probabilistic; we use a generous bound here.
        The benchmark script captures fine-grained std-dev numbers.
        """
        dist = populated_ring.arc_distribution()
        for url, fraction in dist.items():
            assert fraction < 0.80, f"{url} holds {fraction:.1%} — too unbalanced"

    def test_more_replicas_better_balance(self):
        """On average, higher replica counts produce better balance.
        We verify this by averaging over multiple independent rings.
        """
        import statistics

        def _max_arc(replicas: int, n_nodes: int, trials: int = 5) -> float:
            maxes = []
            for seed in range(trials):
                r = ConsistentHashRing(replicas=replicas)
                for i in range(n_nodes):
                    r.add_node(f"http://agent-{seed}-{i}:8001")
                maxes.append(max(r.arc_distribution().values()))
            return statistics.mean(maxes)

        avg_low = _max_arc(replicas=10, n_nodes=3)
        avg_high = _max_arc(replicas=200, n_nodes=3)
        assert avg_high < avg_low, (
            f"Expected higher replicas to improve balance on average: "
            f"replicas=10 avg_max={avg_low:.3f}, replicas=200 avg_max={avg_high:.3f}"
        )


# ---------------------------------------------------------------------------
# Repr
# ---------------------------------------------------------------------------

def test_repr(populated_ring):
    r = repr(populated_ring)
    assert "ConsistentHashRing" in r
    assert "nodes=3" in r
