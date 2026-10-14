"""Panel application factory."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import PRODUCT, __version__
from .api.routes import api_router, subscriptions
from .config import get_settings
from .core.logging import configure_logging, get_logger
from .db import healthcheck, init_engine, run_migrations
from .middleware import (
    IpAllowlistMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    StaticCacheMiddleware,
)
from .services import scheduler

log = get_logger("main")

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.mode == "panel":
        scheduler.start()
    else:
        log.info("running in node mode: the control plane is disabled")
    log.info(
        "%s %s ready on %s:%s (mode=%s)",
        PRODUCT,
        __version__,
        settings.host,
        settings.port,
        settings.mode,
    )
    try:
        yield
    finally:
        scheduler.stop()
        log.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, filename="panah.log")
    init_engine(settings.database_path)
    run_migrations()

    app = FastAPI(
        title=f"{PRODUCT} Panel",
        version=__version__,
        description="OpenVPN control plane with node, tunnel and client management.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # Middleware runs outermost first, so security headers are applied last.
    app.add_middleware(StaticCacheMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(IpAllowlistMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(api_router)
    app.include_router(subscriptions.router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, Any]:
        return {
            "status": "ok" if healthcheck() else "degraded",
            "product": PRODUCT,
            "version": __version__,
            "mode": settings.mode,
        }

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> Any:
        # Keep the panel out of search indexes.
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    _mount_frontend(app)
    _install_error_handlers(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built single-page application."""
    if not STATIC_DIR.is_dir():
        log.warning("no built frontend found at %s; API only", STATIC_DIR)

        @app.get("/", include_in_schema=False)
        def placeholder() -> dict[str, str]:
            return {
                "product": PRODUCT,
                "version": __version__,
                "message": "the frontend has not been built; see README for instructions",
            }

        return

    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> Any:
        # API and subscription prefixes are handled by their routers already.
        if full_path.startswith(("api/", "sub/", "agent/")):
            raise HTTPException(status_code=404, detail="not found")

        candidate = (STATIC_DIR / full_path).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            # Path traversal attempt: fall through to the SPA shell.
            candidate = INDEX_FILE
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        if INDEX_FILE.is_file():
            return FileResponse(INDEX_FILE)
        raise HTTPException(status_code=404, detail="not found")


def _safe_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Make a validation report JSON serialisable.

    Pydantic puts the offending value and any exception raised by a validator
    into each entry, and neither is guaranteed to be serialisable — returning
    them raw turns a 422 into a 500 from inside the error handler itself.
    """
    cleaned: list[dict[str, Any]] = []
    for error in exc.errors()[:20]:
        item: dict[str, Any] = {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "invalid value"))[:300],
            "type": str(error.get("type", "value_error")),
        }
        if "input" in error:
            item["input"] = str(error["input"])[:200]
        context = error.get("ctx") or {}
        if context:
            item["context"] = {str(key): str(value)[:200] for key, value in context.items()}
        cleaned.append(item)
    return cleaned


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = _safe_errors(exc)
        log.info("rejected %s %s: %s", request.method, request.url.path, errors[:3])
        return JSONResponse(
            status_code=422,
            content={
                "detail": "invalid request",
                "errors": errors,
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        headers = getattr(exc, "headers", None)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "request_id": getattr(request.state, "request_id", "")},
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        log.exception("unhandled error on %s (request %s)", request.url.path, request_id)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "request_id": request_id},
        )


def settings_snapshot() -> dict[str, object]:
    """Used by the CLI to print a redacted configuration summary."""
    from .config import settings_for_repr

    return settings_for_repr()


app = None


def get_app() -> FastAPI:
    """Lazy ASGI app for ``uvicorn panel.main:get_app --factory``."""
    global app
    if app is None:
        app = create_app()
    return app


def run() -> None:  # pragma: no cover - exercised by the installer
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "panel.main:get_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=",".join(settings.trusted_proxy_list),
        server_header=False,
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    with contextlib.suppress(KeyboardInterrupt):
        run()
