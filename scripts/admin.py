#!/usr/bin/env python3
"""
scripts/admin.py
~~~~~~~~~~~~~~~~
CLI utility for managing a running Context-Ring proxy.

Commands:
  register    <url> [url ...]    Register one or more agent nodes
  deregister  <url> [url ...]    Evict one or more agent nodes
  status                         Print ring status and arc distribution
  route       <session_id>       Show which node a session routes to
  nodes                          List all registered nodes
  health                         Check proxy liveness

Usage:
  python scripts/admin.py --proxy http://localhost:8000 --key dev-key status
  python scripts/admin.py register http://agent-1:8001 http://agent-2:8001
  python scripts/admin.py route user-session-abc123
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PROXY_URL = os.getenv("CONTEXT_RING_PROXY_URL", "http://localhost:8000")
API_KEY = os.getenv("CONTEXT_RING_API_KEY", "")


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _request(
    method: str,
    path: str,
    body: dict | None = None,
    api_key: str = "",
) -> dict:
    url = f"{PROXY_URL.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        try:
            detail = json.loads(body_text).get("detail", body_text)
        except Exception:
            detail = body_text
        print(f"  ✗  HTTP {e.code}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"  ✗  Connection error: {e.reason}", file=sys.stderr)
        print(f"     Is the proxy running at {PROXY_URL}?", file=sys.stderr)
        sys.exit(1)


def _print_json(data: dict) -> None:
    print(json.dumps(data, indent=2))


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_register(urls: list[str], key: str) -> None:
    for url in urls:
        result = _request("POST", "/v1/register", {"agent_url": url}, api_key=key)
        print(f"  ✓  {result['status']:10}  {url}  ({result['vnodes']} vnodes, "
              f"ring size: {result['ring_size']})")


def cmd_deregister(urls: list[str], key: str) -> None:
    for url in urls:
        result = _request("POST", "/v1/deregister", {"agent_url": url}, api_key=key)
        print(f"  ✓  {result['status']:10}  {url}  "
              f"(orphaned sessions: {result['orphaned_sessions']})")


def cmd_status() -> None:
    data = _request("GET", "/v1/ring/status")
    healthy = "✓ Healthy" if data["healthy"] else "✗ Degraded"
    uptime = data["uptime_seconds"]
    h, rem = divmod(int(uptime), 3600)
    m, s = divmod(rem, 60)

    print(f"\n  Status  : {healthy}")
    print(f"  Nodes   : {data['node_count']}")
    print(f"  Vnodes  : {data['vnode_count']}")
    print(f"  Uptime  : {h:02d}:{m:02d}:{s:02d}")
    print()

    if data["nodes"]:
        print(f"  {'Node URL':<40} {'Sessions':>9} {'Routed':>9} {'Arc %':>7} {'Vnodes':>7}")
        print(f"  {'─' * 40} {'─' * 9} {'─' * 9} {'─' * 7} {'─' * 7}")
        for n in sorted(data["nodes"], key=lambda x: x["arc_fraction"], reverse=True):
            bar_len = int(n["arc_fraction"] * 20)
            bar = "█" * bar_len + "░" * (20 - bar_len)
            arc_pct = n["arc_fraction"] * 100
            print(f"  {n['url']:<40} {n['active_sessions']:>9} {n['total_routed']:>9} "
                  f"{arc_pct:>6.1f}% {n['vnode_count']:>7}")
            print(f"  {bar}")
    print()


def cmd_nodes() -> None:
    data = _request("GET", "/v1/ring/nodes")
    nodes = data.get("nodes", [])
    if not nodes:
        print("  No nodes registered.")
        return
    for n in nodes:
        print(f"  {n['url']}")
        print(f"    sessions={n['active_sessions']}  "
              f"total_routed={n['total_routed']}  "
              f"arc={n['arc_pct']:.2f}%  "
              f"vnodes={n['vnodes']}")


def cmd_route(session_id: str) -> None:
    """Simulate routing without sending a real completion request."""
    # We use the status endpoint to get node list, then do local routing
    # (avoids needing a real completion payload)
    data = _request("GET", "/v1/ring/nodes")
    nodes = [n["url"] for n in data.get("nodes", [])]
    if not nodes:
        print("  No nodes registered — cannot route.")
        return

    # Re-implement O(log N) locally to show the result without side effects
    import bisect
    try:
        import mmh3
        h = mmh3.hash(session_id, signed=False)
    except ImportError:
        print("  mmh3 not installed locally — install with: pip install mmh3")
        return

    replicas = 128  # assume default; proxy is authoritative
    ring: list[tuple[int, str]] = []
    for url in nodes:
        for i in range(replicas):
            vh = mmh3.hash(f"{url}#vnode-{i}", signed=False)
            bisect.insort(ring, (vh, url))

    idx = bisect.bisect_right([x[0] for x in ring], h)
    if idx == len(ring):
        idx = 0
    target_hash, target_node = ring[idx]

    print(f"\n  Session ID  : {session_id}")
    print(f"  Hash        : 0x{h:08x}  ({h})")
    print(f"  Target node : {target_node}")
    print(f"  Vnode hash  : 0x{target_hash:08x}")
    print()


def cmd_health() -> None:
    data = _request("GET", "/healthz")
    if data.get("status") == "ok":
        print(f"  ✓  Proxy is healthy  (nodes: {data.get('nodes', '?')})")
    else:
        print(f"  ✗  Proxy is degraded: {data}")
        sys.exit(1)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Context-Ring admin CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--proxy", default=PROXY_URL,
        help=f"Proxy base URL (env: CONTEXT_RING_PROXY_URL) [default: {PROXY_URL}]",
    )
    parser.add_argument(
        "--key", default=API_KEY,
        help="Admin API key (env: CONTEXT_RING_API_KEY)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_reg = sub.add_parser("register", help="Register agent node(s)")
    p_reg.add_argument("urls", nargs="+", metavar="URL")

    p_dereg = sub.add_parser("deregister", help="Evict agent node(s)")
    p_dereg.add_argument("urls", nargs="+", metavar="URL")

    sub.add_parser("status", help="Ring status and distribution")
    sub.add_parser("nodes", help="List registered nodes")
    sub.add_parser("health", help="Liveness check")

    p_route = sub.add_parser("route", help="Show routing for a session ID")
    p_route.add_argument("session_id", metavar="SESSION_ID")

    args = parser.parse_args()

    global PROXY_URL
    PROXY_URL = args.proxy.rstrip("/")

    if args.command == "register":
        cmd_register(args.urls, args.key)
    elif args.command == "deregister":
        cmd_deregister(args.urls, args.key)
    elif args.command == "status":
        cmd_status()
    elif args.command == "nodes":
        cmd_nodes()
    elif args.command == "health":
        cmd_health()
    elif args.command == "route":
        cmd_route(args.session_id)


if __name__ == "__main__":
    main()
