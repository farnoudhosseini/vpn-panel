"""Node agent.

Deliberately small: one authenticated endpoint that dispatches the same
operations the panel runs locally. There is no shell, no file browser and no
arbitrary path write — every plan is validated against the managed roots before
anything touches the filesystem.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import PRODUCT, __version__
from ..config import get_settings
from ..core.logging import configure_logging, get_logger
from ..core.ratelimit import RateLimiter
from ..core.security import constant_time_equals
from ..services import node_ops, tunnels
from ..services import plan as plan_mod

log = get_logger("agent")

AGENT_OPS = {
    "capabilities": lambda payload: node_ops.capabilities(),
    "metrics": lambda payload: node_ops.metrics(),
    "openvpn.status": lambda payload: {
        "sessions": node_ops.openvpn_sessions(_require(payload, "tag"))
    },
    "openvpn.disconnect": lambda payload: node_ops.openvpn_disconnect(
        _require(payload, "tag"), _require(payload, "common_name")
    ),
    "plan.apply": lambda payload: node_ops.apply_plan(
        plan_mod.SidePlan.from_dict(_require_dict(payload, "plan"))
    ),
    "inbound.remove": lambda payload: node_ops.remove_inbound(_require_dict(payload, "inbound")),
    "tunnel.plan": lambda payload: {"plan": node_ops.plan_tunnel_side(payload).to_dict()},
    "tunnel.teardown": lambda payload: node_ops.teardown_tunnel(payload),
    "nginx.apply": lambda payload: node_ops.apply_nginx(payload),
    "nginx.remove": lambda payload: node_ops.remove_nginx(payload),
    "nginx.status": lambda payload: node_ops.nginx_status(),
    "backpack.info": lambda payload: tunnels.backpack_info(),
    "service.state": lambda payload: node_ops.service_status(_require(payload, "unit")),
}


class OperationError(Exception):
    """Raised when a request is well-formed but not permitted."""


def _require(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OperationError(f"missing required field {key!r}")
    return value.strip()


def _require_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise OperationError(f"missing required object {key!r}")
    return value


def create_agent_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, filename="panah-agent.log")

    if not settings.agent_key:
        # Fail closed: an agent with no key would accept anyone.
        raise RuntimeError(
            "PANAH_AGENT_KEY is not configured; refusing to start the node agent"
        )

    app = FastAPI(
        title=f"{PRODUCT} Node Agent",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["POST", "GET"],
        allow_headers=["X-Agent-Key", "X-Agent-Version", "Content-Type"],
    )

    limiter = RateLimiter(limit=600, window_seconds=60)
    _failures: dict[str, deque[float]] = defaultdict(deque)

    @app.middleware("http")
    async def hardening(request: Request, call_next):  # type: ignore[no-untyped-def]
        client_ip = request.client.host if request.client else "unknown"
        allowed, retry_after = limiter.check(client_ip)
        if not allowed:
            return JSONResponse(
                {"error": "rate limited"},
                status_code=429,
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _authorised(provided: str | None, client_ip: str) -> tuple[bool, str]:
        now = time.monotonic()
        window = _failures[client_ip]
        while window and now - window[0] > 300:
            window.popleft()
        if len(window) >= 20:
            return False, "too many authentication failures"
        if not provided or not constant_time_equals(provided, settings.agent_key):
            window.append(now)
            return False, "invalid API key"
        window.clear()
        return True, ""

    @app.get("/agent/v1/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "product": PRODUCT, "version": __version__}

    @app.post("/agent/v1/ops")
    def ops(
        body: dict[str, Any],
        request: Request,
        x_agent_key: str | None = Header(default=None, alias="X-Agent-Key"),
    ) -> JSONResponse:
        client_ip = request.client.host if request.client else "unknown"
        ok, reason = _authorised(x_agent_key, client_ip)
        if not ok:
            log.warning("rejected agent request from %s: %s", client_ip, reason)
            return JSONResponse({"error": reason}, status_code=401)

        op = body.get("op")
        if not isinstance(op, str) or op not in AGENT_OPS:
            return JSONResponse({"error": f"unsupported operation {op!r}"}, status_code=400)

        payload = body.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse({"error": "payload must be an object"}, status_code=400)

        try:
            result = AGENT_OPS[op](payload)
        except OperationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except plan_mod.PlanError as exc:
            log.warning("plan rejected for op %s: %s", op, exc)
            return JSONResponse({"error": f"plan rejected: {exc}"}, status_code=400)
        except Exception as exc:
            log.exception("operation %s failed", op)
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

        return JSONResponse({"result": result})

    @app.get("/")
    def root() -> JSONResponse:
        # Nothing to discover: the agent is not a browsable service.
        return JSONResponse({"error": "not found"}, status_code=404)

    log.info("node agent ready on %s:%s", settings.host, settings.agent_port)
    return app
