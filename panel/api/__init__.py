"""HTTP API for the panel."""

from __future__ import annotations

__all__ = ["router"]


def router():  # pragma: no cover - convenience re-export
    from .routes import api_router

    return api_router
