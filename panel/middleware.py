"""HTTP middleware: request identity, hardening headers, allowlist and limits."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import get_settings
from .core.logging import get_logger
from .core.ratelimit import RateLimiter, ip_in_allowlist
from .services import paths

log = get_logger("http")

REQUEST_ID_HEADER = "X-Request-ID"

# Paths that must stay reachable without an allowlist entry or rate limiting.
OPEN_PATHS = {"/health", "/api/system/health", "/favicon.ico", "/robots.txt"}

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers that remove whole classes of browser-side attack."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault(
            "Permissions-Policy",
            "accelerometer=(), camera=(), geolocation=(), microphone=(), payment=()",
        )
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")

        path = request.url.path
        if path.startswith("/api") or path.startswith("/sub") or path.startswith("/agent"):
            headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            headers.setdefault("Cache-Control", "no-store")
        else:
            headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)

        if get_settings().cookie_secure:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id and write one structured access log line."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = (time.perf_counter() - started) * 1000
            log.exception(
                "unhandled error %s %s (%.1fms)",
                request.method,
                request.url.path,
                duration,
                extra={"request_id": request_id},
            )
            raise
        duration = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

        if not request.url.path.startswith(("/assets", "/static")):
            log.info(
                "%s %s -> %s (%.1fms)",
                request.method,
                request.url.path,
                response.status_code,
                duration,
                extra={"request_id": request_id},
            )
        return response


class IpAllowlistMiddleware(BaseHTTPMiddleware):
    """Optional hard allowlist for the whole panel."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        entries = get_settings().ip_allowlist_entries
        if not entries or request.url.path in OPEN_PATHS:
            return await call_next(request)

        settings = get_settings()
        peer = request.client.host if request.client else ""
        if peer in set(settings.trusted_proxy_list):
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                peer = forwarded.split(",")[0].strip()

        if not ip_in_allowlist(peer, entries):
            log.warning("allowlist blocked %s for %s", peer, request.url.path)
            return JSONResponse({"detail": "not allowed"}, status_code=403)
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Coarse per-IP limit; authentication endpoints have their own limiter."""

    def __init__(self, app, *, limit: int | None = None) -> None:
        super().__init__(app)
        configured = limit or get_settings().rate_limit_per_minute
        self.limiter = RateLimiter(limit=configured, window_seconds=60)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in OPEN_PATHS or path.startswith(("/assets", "/static")):
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        allowed, retry_after = self.limiter.check(f"{client}:{path.split('/')[2] if path.count('/') > 2 else path}")
        if not allowed:
            return JSONResponse(
                {"detail": "too many requests"},
                status_code=429,
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        return await call_next(request)


class StaticCacheMiddleware(BaseHTTPMiddleware):
    """Long-lived hashed assets, never-cached HTML."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        path = request.url.path
        if path.startswith(("/assets/", "/static/")):
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        elif not path.startswith(("/api", "/sub", "/agent")):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response


def prune_limiter(app: object) -> int:
    """Called by the maintenance job to drop idle buckets."""
    from .core.ratelimit import RateLimiter as _Limiter

    removed = 0
    for value in vars(app).values() if app is not None else []:
        if isinstance(value, _Limiter):
            removed += value.prune()
    return removed


def state_dir() -> str:
    return str(paths.run_dir())
