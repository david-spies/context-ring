"""
tests.test_proxy
~~~~~~~~~~~~~~~~
Integration tests for the Context-Ring FastAPI proxy application.

Uses httpx.AsyncClient with ASGITransport so no real network is needed.
Agent workers are replaced by a simple mock ASGI app.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

import pytest
import pytest_asyncio
import httpx
from httpx import AsyncClient, ASGITransport

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Patch env before importing main so lifespan seeds no nodes
os.environ.setdefault("INITIAL_AGENT_NODES", "")
os.environ.setdefault("CONTEXT_RING_API_KEY", "test-secret-key")
os.environ.setdefault("VNODE_REPLICAS", "50")

from main import app


# ---------------------------------------------------------------------------
# Mock agent worker (ASGI)
# ---------------------------------------------------------------------------

async def mock_agent_app(scope, receive, send):
    """Minimal ASGI app that echoes routing headers back as JSON."""
    assert scope["type"] == "http"
    body = b""
    while True:
        event = await receive()
        body += event.get("body", b"")
        if not event.get("more_body"):
            break
    payload = json.loads(body or b"{}")
    response_body = json.dumps({
        "object": "chat.completion",
        "agent": scope.get("server", ("unknown", 0))[0],
        "session_id": payload.get("session_id"),
        "echoed": True,
    }).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [[b"content-type", b"application/json"]]})
    await send({"type": "http.response.body", "body": response_body})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


ADMIN_HEADERS = {"X-Api-Key": "test-secret-key"}


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_healthz_no_nodes(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_healthz_with_nodes(client):
    await client.post("/v1/register",
                      json={"agent_url": "http://agent-1:8001"},
                      headers=ADMIN_HEADERS)
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    # cleanup
    await client.post("/v1/deregister",
                      json={"agent_url": "http://agent-1:8001"},
                      headers=ADMIN_HEADERS)


# ---------------------------------------------------------------------------
# /v1/register & /v1/deregister
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_node(client):
    resp = await client.post("/v1/register",
                             json={"agent_url": "http://agent-reg-1:8001"},
                             headers=ADMIN_HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "mounted"
    assert data["vnodes"] == 50
    # cleanup
    await client.post("/v1/deregister",
                      json={"agent_url": "http://agent-reg-1:8001"},
                      headers=ADMIN_HEADERS)


@pytest.mark.asyncio
async def test_register_duplicate_returns_409(client):
    url = "http://agent-dup:8001"
    await client.post("/v1/register", json={"agent_url": url}, headers=ADMIN_HEADERS)
    resp = await client.post("/v1/register", json={"agent_url": url}, headers=ADMIN_HEADERS)
    assert resp.status_code == 409
    # cleanup
    await client.post("/v1/deregister", json={"agent_url": url}, headers=ADMIN_HEADERS)


@pytest.mark.asyncio
async def test_deregister_unknown_returns_404(client):
    resp = await client.post("/v1/deregister",
                             json={"agent_url": "http://ghost:9999"},
                             headers=ADMIN_HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_register_requires_api_key(client):
    resp = await client.post("/v1/register", json={"agent_url": "http://agent-x:8001"})
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# /v1/chat/completions routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_missing_session_id_returns_400(client):
    await client.post("/v1/register",
                      json={"agent_url": "http://agent-r:8001"},
                      headers=ADMIN_HEADERS)
    resp = await client.post("/v1/chat/completions",
                             json={"model": "gpt-4", "messages": []})
    assert resp.status_code == 400
    assert "session" in resp.json()["detail"].lower()
    await client.post("/v1/deregister",
                      json={"agent_url": "http://agent-r:8001"},
                      headers=ADMIN_HEADERS)


@pytest.mark.asyncio
async def test_route_no_nodes_returns_503(client):
    resp = await client.post("/v1/chat/completions",
                             json={"session_id": "s1", "messages": []})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_malformed_json_returns_400(client):
    await client.post("/v1/register",
                      json={"agent_url": "http://agent-m:8001"},
                      headers=ADMIN_HEADERS)
    resp = await client.post(
        "/v1/chat/completions",
        content=b"NOT_JSON",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    await client.post("/v1/deregister",
                      json={"agent_url": "http://agent-m:8001"},
                      headers=ADMIN_HEADERS)


@pytest.mark.asyncio
async def test_x_session_id_header_accepted(client):
    """Route via X-Session-ID header instead of JSON body field."""
    await client.post("/v1/register",
                      json={"agent_url": "http://agent-h:8001"},
                      headers=ADMIN_HEADERS)
    # This will 502 because no real worker is running, but routing must proceed past 400
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": []},
        headers={"X-Session-ID": "header-session-abc"},
    )
    # 502 = routing succeeded but agent unreachable (expected with no real worker)
    assert resp.status_code in (200, 502, 504), f"Unexpected: {resp.status_code}"
    await client.post("/v1/deregister",
                      json={"agent_url": "http://agent-h:8001"},
                      headers=ADMIN_HEADERS)


# ---------------------------------------------------------------------------
# /v1/ring/status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ring_status_structure(client):
    resp = await client.get("/v1/ring/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "healthy" in data
    assert "node_count" in data
    assert "vnode_count" in data
    assert "uptime_seconds" in data
    assert isinstance(data["nodes"], list)


@pytest.mark.asyncio
async def test_ring_status_arc_fractions_sum_to_one(client):
    urls = ["http://agent-a1:8001", "http://agent-a2:8002", "http://agent-a3:8003"]
    for url in urls:
        await client.post("/v1/register", json={"agent_url": url}, headers=ADMIN_HEADERS)

    resp = await client.get("/v1/ring/status")
    data = resp.json()
    total = sum(n["arc_fraction"] for n in data["nodes"])
    assert abs(total - 1.0) < 0.01

    for url in urls:
        await client.post("/v1/deregister", json={"agent_url": url}, headers=ADMIN_HEADERS)


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_endpoint(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "context_ring_nodes_total" in resp.text
    assert "context_ring_requests_total" in resp.text
    assert "context_ring_uptime_seconds" in resp.text


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_security_headers_present(client):
    resp = await client.get("/healthz")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "cache-control" in resp.headers
