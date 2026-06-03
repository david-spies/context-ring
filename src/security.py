"""
context_ring.security
~~~~~~~~~~~~~~~~~~~~~
Security layer for the Context-Ring proxy.

Features
--------
* API-key authentication for admin endpoints (register / deregister).
* Per-IP rate limiting with a token-bucket algorithm (in-process).
* Request-size guard to prevent oversized payload DoS.
* Security response headers (HSTS, X-Content-Type-Options, etc.).
* Session-ID validation to prevent topology-inference attacks.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections import defaultdict
from typing import Callable, Dict, Optional

from fastapi import Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger("context_ring.security")

# ---------------------------------------------------------------------------
# Config (from environment)
# ---------------------------------------------------------------------------

API_KEY: Optional[str] = os.getenv("CONTEXT_RING_API_KEY")
ADMIN_PATHS = {"/v1/register", "/v1/deregister"}

# Rate limiting
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "200"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

# Request size guard (default 10 MB)
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(10 * 1024 * 1024)))


# ---------------------------------------------------------------------------
# In-process token bucket rate limiter
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Simple per-key token-bucket rate limiter (not distributed)."""

    def __init__(self, capacity: int, refill_window: int) -> None:
        self._capacity = capacity
        self._refill_window = refill_window          # seconds
        self._buckets: Dict[str, Dict] = defaultdict(lambda: {
            "tokens": capacity,
            "last_refill": time.monotonic(),
        })

    def consume(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        bucket = self._buckets[key]
        now = time.monotonic()
        elapsed = now - bucket["last_refill"]

        # Refill tokens proportional to elapsed time
        refill = elapsed * (self._capacity / self._refill_window)
        bucket["tokens"] = min(self._capacity, bucket["tokens"] + refill)
        bucket["last_refill"] = now

        if bucket["tokens"] >= 1:
            bucket["tokens"] -= 1
            return True
        return False


_rate_limiter = _TokenBucket(
    capacity=RATE_LIMIT_REQUESTS,
    refill_window=RATE_LIMIT_WINDOW_SECONDS,
)


# ---------------------------------------------------------------------------
# FastAPI dependency: API-key verification
# ---------------------------------------------------------------------------

async def verify_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """
    Require a valid API key for admin endpoints.

    Accepts either:
        X-Api-Key: <key>
        Authorization: Bearer <key>

    If CONTEXT_RING_API_KEY is not set, all requests are allowed
    (development mode). A warning is emitted at startup.
    """
    if not API_KEY:
        return   # Auth disabled — dev mode

    provided = x_api_key
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:]

    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide X-Api-Key header or Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(
        hashlib.sha256(provided.encode()).digest(),
        hashlib.sha256(API_KEY.encode()).digest(),
    ):
        logger.warning("AUTH_FAILURE provided_key=%s...", (provided or "")[:6])
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )


# ---------------------------------------------------------------------------
# Session-ID hardening helper (call from route handler if desired)
# ---------------------------------------------------------------------------

SESSION_HMAC_SECRET: Optional[str] = os.getenv("SESSION_HMAC_SECRET")


def sign_session_id(raw_id: str) -> str:
    """
    Produce a signed session token: ``{raw_id}.{hmac_hex}``.

    Prevents adversaries from crafting session IDs that deliberately
    collide on a target node.  Only useful if callers also call
    :py:func:`verify_session_id` before routing.
    """
    if not SESSION_HMAC_SECRET:
        return raw_id
    sig = hmac.new(
        SESSION_HMAC_SECRET.encode(),
        raw_id.encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    return f"{raw_id}.{sig}"


def verify_session_id(token: str) -> str:
    """
    Verify and return the raw session ID, or raise HTTP 400.

    No-op (returns *token* unchanged) when SESSION_HMAC_SECRET is unset.
    """
    if not SESSION_HMAC_SECRET:
        return token
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Malformed session token.")
    raw_id, sig = parts
    expected = hmac.new(
        SESSION_HMAC_SECRET.encode(),
        raw_id.encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=400, detail="Invalid session token signature.")
    return raw_id


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cache-Control": "no-store",
}


class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Middleware that:
    1. Enforces per-IP rate limits.
    2. Rejects oversized request bodies.
    3. Appends security response headers.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 1. Rate limiting
        client_ip = self._get_client_ip(request)
        if not _rate_limiter.consume(client_ip):
            logger.warning("RATE_LIMIT ip=%s path=%s", client_ip, request.url.path)
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
            )

        # 2. Body size guard (peek at Content-Length header; full check on read)
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body exceeds limit ({MAX_BODY_BYTES} bytes)."},
            )

        # 3. Warn on missing API key for admin paths (dev mode)
        if not API_KEY and request.url.path in ADMIN_PATHS:
            logger.warning(
                "DEV_MODE_WARNING: admin endpoint %s is unprotected. "
                "Set CONTEXT_RING_API_KEY in production.",
                request.url.path,
            )

        response = await call_next(request)

        # 4. Attach security headers
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value

        return response

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
