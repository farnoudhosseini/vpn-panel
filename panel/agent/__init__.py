"""Node agent: the lightweight service installed on secondary servers."""

from __future__ import annotations

__all__ = ["create_agent_app"]


def create_agent_app(*args, **kwargs):  # pragma: no cover - re-export shim
    from .app import create_agent_app as factory

    return factory(*args, **kwargs)
