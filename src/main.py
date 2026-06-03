"""
context_ring.main
~~~~~~~~~~~~~~~~~
Production FastAPI reverse-proxy application.

Endpoints
---------
POST /v1/register          Register an agent node
POST /v1/deregister        Gracefully evict an agent node
POST /v1/chat/completions  Proxy an LLM completion request
GET  /v1/ring/status       Cluster health + arc distribution
GET  /v1/ring/nodes        List all registered nodes with metadata
GET  /healthz              Liveness probe (K8s/Docker compatible)
GET  /metrics              Prometheus-compatible text metrics
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .manager import RingManager
from .security import SecurityMiddleware, verify_api_key

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("context_ring.main")

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

REPLICAS = int(os.getenv("VNODE_REPLICAS", "128"))
REDIS_URL = os.getenv("REDIS_URL")
INITIAL_NODES_RAW = os.getenv("INITIAL_AGENT_NODES", "")
PROXY_TIMEOUT = float(os.getenv("PROXY_TIMEOUT_SECONDS", "60"))
PROXY_CONNECT_TIMEOUT = float(os.getenv("PROXY_CONNECT_TIMEOUT_SECONDS", "5"))
MAX_KEEPALIVE = int(os.getenv("HTTP_MAX_KEEPALIVE", "50"))
MAX_CONNECTIONS = int(os.getenv("HTTP_MAX_CONNECTIONS", "200"))

ring_manager: RingManager = None       # initialised in lifespan
http_client: httpx.AsyncClient = None  # initialised in lifespan
_start_time: float = 0.0
_request_counter: Dict[str, int] = {"total": 0, "routed": 0, "errors": 0}

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global ring_manager, http_client, _start_time

    _start_time = time.monotonic()
    logger.info("Context-Ring proxy starting up — replicas=%d", REPLICAS)

    ring_manager = RingManager(replicas=REPLICAS, redis_url=REDIS_URL)
    initial_nodes = [n for n in INITIAL_NODES_RAW.split(",") if n.strip()]
    await ring_manager.startup(initial_nodes)

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(PROXY_TIMEOUT, connect=PROXY_CONNECT_TIMEOUT),
        limits=httpx.Limits(
            max_keepalive_connections=MAX_KEEPALIVE,
            max_connections=MAX_CONNECTIONS,
        ),
        follow_redirects=False,
    )

    logger.info(
        "Proxy ready — nodes=%d vnodes=%d",
        ring_manager.node_count,
        ring_manager.vnode_count,
    )
    yield

    logger.info("Context-Ring proxy shutting down.")
    await ring_manager.shutdown()
    await http_client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Context-Ring Proxy",
    description=(
        "State-preserving consistent-hash load balancer for AI agent swarms. "
        "Routes LLM sessions to the same agent instance on every turn, "
        "maximising local context-cache hits and slashing token costs."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)
app.add_middleware(SecurityMiddleware)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class NodeRegistration(BaseModel):
    agent_url: str = Field(..., description="Full base URL of the agent worker, e.g. http://agent-1:8001")

    @field_validator("agent_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class NodeInfo(BaseModel):
    url: str
    active_sessions: int
    total_routed: int
    arc_fraction: float
    vnode_count: int


class RingStatus(BaseModel):
    healthy: bool
    node_count: int
    vnode_count: int
    uptime_seconds: float
    nodes: List[NodeInfo]


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def get_ring() -> RingManager:
    if ring_manager is None:
        raise HTTPException(status_code=503, detail="Ring not initialised.")
    return ring_manager


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/v1/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register an agent node",
    dependencies=[Depends(verify_api_key)],
)
async def register_agent_node(
    body: NodeRegistration,
    mgr: RingManager = Depends(get_ring),
):
    try:
        info = await mgr.add_node(body.agent_url)
        logger.info("REGISTER %s", body.agent_url)
        return {
            "status": "mounted",
            "node": body.agent_url,
            "vnodes": info.replicas,
            "ring_size": mgr.vnode_count,
        }
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post(
    "/v1/deregister",
    status_code=status.HTTP_200_OK,
    summary="Gracefully evict an agent node",
    dependencies=[Depends(verify_api_key)],
)
async def deregister_agent_node(
    body: NodeRegistration,
    mgr: RingManager = Depends(get_ring),
):
    try:
        info = await mgr.remove_node(body.agent_url)
        logger.info("DEREGISTER %s (orphaned sessions: %d)", body.agent_url, info.active_sessions)
        return {
            "status": "evicted",
            "node": body.agent_url,
            "orphaned_sessions": info.active_sessions,
            "ring_size": mgr.vnode_count,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ---------------------------------------------------------------------------
# Main proxy endpoint
# ---------------------------------------------------------------------------

@app.api_route(
    "/v1/chat/completions",
    methods=["POST"],
    summary="Route a chat-completion request to its designated agent",
)
async def route_agent_request(
    request: Request,
    mgr: RingManager = Depends(get_ring),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID"),
):
    _request_counter["total"] += 1

    # 1. Parse body
    try:
        body = await request.json()
    except Exception:
        _request_counter["errors"] += 1
        raise HTTPException(status_code=400, detail="Malformed JSON request body.")

    # 2. Resolve session discriminator
    session_id = body.get("session_id") or x_session_id
    if not session_id:
        _request_counter["errors"] += 1
        raise HTTPException(
            status_code=400,
            detail=(
                "Context-Ring requires a session discriminator. "
                "Provide 'session_id' in the JSON body or an 'X-Session-ID' header."
            ),
        )

    # 3. Hash ring lookup
    result = await mgr.route(session_id)
    if result is None:
        _request_counter["errors"] += 1
        raise HTTPException(
            status_code=503,
            detail="No active agent nodes available in the cluster ring.",
        )

    _request_counter["routed"] += 1
    target_url = f"{result.node.rstrip('/')}{request.url.path}"

    # 4. Build proxy headers
    proxy_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "transfer-encoding"}
    }
    proxy_headers["X-Context-Ring-Node"] = result.node
    proxy_headers["X-Context-Ring-Hash"] = hex(result.key_hash)
    proxy_headers["X-Context-Ring-Vnode"] = str(result.vnode_index)

    logger.debug(
        "ROUTE session=%s → %s (hash=0x%08x)",
        session_id,
        result.node,
        result.key_hash,
    )

    # 5. Async streaming proxy
    try:
        req = http_client.build_request(
            method="POST",
            url=target_url,
            headers=proxy_headers,
            json=body,
        )
        response = await http_client.send(req, stream=True)

        # Strip hop-by-hop headers before forwarding
        excluded = {"transfer-encoding", "connection", "keep-alive", "upgrade"}
        forward_headers = {
            k: v for k, v in response.headers.items()
            if k.lower() not in excluded
        }
        forward_headers["X-Context-Ring-Node"] = result.node

        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=forward_headers,
            background=None,
        )

    except httpx.ConnectError as exc:
        _request_counter["errors"] += 1
        logger.error("CONNECT_ERROR node=%s err=%s", result.node, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Target agent node unreachable: {result.node}. Context migration pending.",
        )
    except httpx.TimeoutException as exc:
        _request_counter["errors"] += 1
        logger.error("TIMEOUT node=%s err=%s", result.node, exc)
        raise HTTPException(status_code=504, detail=f"Upstream timeout: {result.node}")
    except httpx.RequestError as exc:
        _request_counter["errors"] += 1
        logger.error("PROXY_ERROR node=%s err=%s", result.node, exc)
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

@app.get("/v1/ring/status", summary="Cluster health and arc distribution")
async def ring_status(mgr: RingManager = Depends(get_ring)) -> RingStatus:
    arcs = mgr.arc_distribution()
    nodes_info: List[NodeInfo] = []
    for url, info in mgr.all_node_info().items():
        nodes_info.append(NodeInfo(
            url=url,
            active_sessions=info.active_sessions,
            total_routed=info.total_routed,
            arc_fraction=round(arcs.get(url, 0.0), 4),
            vnode_count=len(info.vnodes),
        ))
    return RingStatus(
        healthy=mgr.node_count > 0,
        node_count=mgr.node_count,
        vnode_count=mgr.vnode_count,
        uptime_seconds=round(time.monotonic() - _start_time, 2),
        nodes=nodes_info,
    )


@app.get("/v1/ring/nodes", summary="List registered nodes")
async def list_nodes(mgr: RingManager = Depends(get_ring)):
    arcs = mgr.arc_distribution()
    return {
        "nodes": [
            {
                "url": url,
                "active_sessions": info.active_sessions,
                "total_routed": info.total_routed,
                "arc_pct": round(arcs.get(url, 0.0) * 100, 2),
                "vnodes": len(info.vnodes),
            }
            for url, info in mgr.all_node_info().items()
        ]
    }


@app.get("/healthz", summary="Liveness probe")
async def healthz(mgr: RingManager = Depends(get_ring)):
    if mgr.node_count == 0:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "reason": "no_nodes"},
        )
    return {"status": "ok", "nodes": mgr.node_count}


@app.get("/metrics", response_class=PlainTextResponse, summary="Prometheus metrics")
async def prometheus_metrics(mgr: RingManager = Depends(get_ring)):
    uptime = round(time.monotonic() - _start_time, 2)
    lines = [
        "# HELP context_ring_nodes_total Active agent nodes in the ring",
        "# TYPE context_ring_nodes_total gauge",
        f"context_ring_nodes_total {mgr.node_count}",
        "# HELP context_ring_vnodes_total Total virtual nodes in the ring",
        "# TYPE context_ring_vnodes_total gauge",
        f"context_ring_vnodes_total {mgr.vnode_count}",
        "# HELP context_ring_requests_total Proxy requests by outcome",
        "# TYPE context_ring_requests_total counter",
        f'context_ring_requests_total{{outcome="routed"}} {_request_counter["routed"]}',
        f'context_ring_requests_total{{outcome="error"}} {_request_counter["errors"]}',
        f'context_ring_requests_total{{outcome="total"}} {_request_counter["total"]}',
        "# HELP context_ring_uptime_seconds Proxy uptime in seconds",
        "# TYPE context_ring_uptime_seconds gauge",
        f"context_ring_uptime_seconds {uptime}",
    ]
    for url, info in mgr.all_node_info().items():
        safe = url.replace('"', '\\"')
        lines += [
            f'context_ring_node_sessions{{node="{safe}"}} {info.active_sessions}',
            f'context_ring_node_routed_total{{node="{safe}"}} {info.total_routed}',
        ]
    return "\n".join(lines) + "\n"
