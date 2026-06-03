"""
scripts/benchmark.py
~~~~~~~~~~~~~~~~~~~~
Benchmarks for the Context-Ring consistent hash ring.

Measures:
  1. Hash throughput          (mmh3 raw, ops/sec)
  2. add_node throughput      (ring mutations/sec)
  3. get_node throughput      (routing lookups/sec)
  4. Arc distribution quality (standard deviation across nodes)
  5. Session stability        (% sessions preserved after scale event)

Run with:
    python scripts/benchmark.py

Or via Makefile:
    make bench
"""

from __future__ import annotations

import os
import sys
import time
import random
import statistics
import string
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ring import ConsistentHashRing


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _random_session_ids(n: int) -> List[str]:
    chars = string.ascii_lowercase + string.digits
    return ["".join(random.choices(chars, k=16)) for _ in range(n)]


def _timer(fn, iterations: int) -> float:
    """Return wall-clock seconds for *iterations* calls to *fn*."""
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    return time.perf_counter() - start


def _fmt(ops_per_sec: float) -> str:
    if ops_per_sec >= 1_000_000:
        return f"{ops_per_sec / 1_000_000:.2f}M ops/sec"
    if ops_per_sec >= 1_000:
        return f"{ops_per_sec / 1_000:.1f}K ops/sec"
    return f"{ops_per_sec:.1f} ops/sec"


def _section(title: str) -> None:
    width = 60
    print(f"\n  {'─' * width}")
    print(f"  {title}")
    print(f"  {'─' * width}")


# ─── Benchmark 1: raw hash throughput ─────────────────────────────────────────

def bench_hash_throughput() -> None:
    _section("1. MurmurHash3 raw throughput")
    ring = ConsistentHashRing(replicas=1)
    ITERS = 500_000
    keys = _random_session_ids(1_000)
    idx = 0

    def _hash_one():
        nonlocal idx
        ring._hash(keys[idx % 1_000])
        idx += 1

    elapsed = _timer(_hash_one, ITERS)
    ops = ITERS / elapsed
    avg_us = (elapsed / ITERS) * 1_000_000
    print(f"  Iterations : {ITERS:,}")
    print(f"  Total time : {elapsed:.3f}s")
    print(f"  Throughput : {_fmt(ops)}")
    print(f"  Avg latency: {avg_us:.3f} µs/hash")


# ─── Benchmark 2: add_node throughput ─────────────────────────────────────────

def bench_add_node() -> None:
    _section("2. add_node throughput (50 replicas)")
    NODES = 200
    ring = ConsistentHashRing(replicas=50)

    start = time.perf_counter()
    for i in range(NODES):
        ring.add_node(f"http://agent-{i}:800{i % 10}")
    elapsed = time.perf_counter() - start

    ops = NODES / elapsed
    print(f"  Nodes added: {NODES}")
    print(f"  Total time : {elapsed:.4f}s")
    print(f"  Throughput : {_fmt(ops)}")
    print(f"  Ring size  : {ring.vnode_count:,} vnodes")


# ─── Benchmark 3: get_node throughput (varying node counts) ───────────────────

def bench_routing_throughput() -> None:
    _section("3. get_node (routing) throughput — O(log N)")
    sessions = _random_session_ids(10_000)
    ITERS = 200_000

    for n_nodes, replicas in [(3, 128), (10, 128), (50, 128), (100, 128)]:
        ring = ConsistentHashRing(replicas=replicas)
        for i in range(n_nodes):
            ring.add_node(f"http://agent-{i}:8001")

        idx = 0

        def _route():
            nonlocal idx
            ring.get_node(sessions[idx % 10_000])
            idx += 1

        elapsed = _timer(_route, ITERS)
        ops = ITERS / elapsed
        avg_us = (elapsed / ITERS) * 1_000_000
        print(f"  {n_nodes:>4} nodes × {replicas} vnodes = {ring.vnode_count:>6,} ring size  "
              f"→  {_fmt(ops):>18}  ({avg_us:.3f} µs/lookup)")


# ─── Benchmark 4: arc distribution quality ────────────────────────────────────

def bench_distribution_quality() -> None:
    _section("4. Arc distribution quality (std deviation %)")
    print(f"  {'Nodes':>6}  {'Replicas':>9}  {'Min %':>7}  {'Max %':>7}  {'Std Dev':>8}  {'Balance'}")

    for n_nodes in [3, 5, 10, 20]:
        for replicas in [50, 128, 200]:
            ring = ConsistentHashRing(replicas=replicas)
            for i in range(n_nodes):
                ring.add_node(f"http://agent-{i}:8001")

            dist = ring.arc_distribution()
            fracs = [v * 100 for v in dist.values()]
            ideal = 100.0 / n_nodes
            std = statistics.stdev(fracs) if len(fracs) > 1 else 0.0
            balance = "✓ Good" if std < ideal * 0.25 else ("~ Fair" if std < ideal * 0.5 else "✗ Poor")

            print(f"  {n_nodes:>6}  {replicas:>9}  "
                  f"{min(fracs):>6.2f}%  {max(fracs):>6.2f}%  "
                  f"{std:>7.2f}%  {balance}")


# ─── Benchmark 5: session stability across scale events ───────────────────────

def bench_session_stability() -> None:
    _section("5. Session stability after scale events")
    SESSIONS = 10_000
    sessions = _random_session_ids(SESSIONS)

    for n_start in [3, 5, 10]:
        ring = ConsistentHashRing(replicas=128)
        for i in range(n_start):
            ring.add_node(f"http://agent-{i}:8001")

        before = {s: ring.get_node(s).node for s in sessions}

        # Scale-out: add one node
        ring.add_node(f"http://agent-{n_start}:8001")
        after_out = {s: ring.get_node(s).node for s in sessions}
        remapped_out = sum(1 for s in sessions if before[s] != after_out[s])
        pct_out = remapped_out / SESSIONS * 100
        expected_out = 100.0 / (n_start + 1)

        # Scale-in: remove the newly added node
        ring.remove_node(f"http://agent-{n_start}:8001")
        after_in = {s: ring.get_node(s).node for s in sessions}
        remapped_in = sum(1 for s in sessions if before[s] != after_in[s])
        pct_in = remapped_in / SESSIONS * 100

        print(f"  {n_start} → {n_start+1} nodes (scale-out): "
              f"{remapped_out:>5,}/{SESSIONS:,} remapped  "
              f"({pct_out:.1f}%  expected ≈{expected_out:.1f}%)")
        print(f"  {n_start+1} → {n_start} nodes (scale-in) : "
              f"{remapped_in:>5,}/{SESSIONS:,} remapped  "
              f"({pct_in:.1f}%  expected ≈ 0.0%)")
        print()


# ─── Benchmark 6: memory footprint ────────────────────────────────────────────

def bench_memory() -> None:
    _section("6. Memory footprint")
    try:
        import tracemalloc
        tracemalloc.start()

        for n_nodes, replicas in [(10, 128), (50, 128), (100, 128), (100, 200)]:
            snapshot_before = tracemalloc.take_snapshot()
            ring = ConsistentHashRing(replicas=replicas)
            for i in range(n_nodes):
                ring.add_node(f"http://agent-{i}:8001")
            snapshot_after = tracemalloc.take_snapshot()

            stats = snapshot_after.compare_to(snapshot_before, "lineno")
            total_kb = sum(s.size_diff for s in stats) / 1024
            print(f"  {n_nodes:>4} nodes × {replicas:>3} replicas = "
                  f"{ring.vnode_count:>6,} vnodes  →  {total_kb:>8.1f} KB")

        tracemalloc.stop()
    except ImportError:
        print("  (tracemalloc unavailable)")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("  ╔══════════════════════════════════════════════════════════╗")
    print("  ║          Context-Ring — Performance Benchmark            ║")
    print("  ╚══════════════════════════════════════════════════════════╝")

    bench_hash_throughput()
    bench_add_node()
    bench_routing_throughput()
    bench_distribution_quality()
    bench_session_stability()
    bench_memory()

    print(f"\n  {'─' * 60}")
    print("  Benchmark complete.\n")


if __name__ == "__main__":
    main()
