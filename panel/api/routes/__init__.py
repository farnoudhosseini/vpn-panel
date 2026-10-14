"""API route modules.

``subscriptions`` is intentionally not listed here: its routes live at the
site root (``/sub/<id>``) because VPN clients consume them directly.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import auth, clients, inbounds, nodes, system, tunnels

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(nodes.router)
api_router.include_router(inbounds.router)
api_router.include_router(clients.router)
api_router.include_router(tunnels.router)
api_router.include_router(system.router)

__all__ = ["api_router"]
