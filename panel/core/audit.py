"""Audit trail and operator notifications.

Every state-changing action funnels through :func:`record` so the panel keeps a
tamper-evident history that is independent of the application log file.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog, Event
from .logging import get_logger

log = get_logger("audit")

# Actions worth surfacing as dashboard events.
NOTIFY_ACTIONS = {
    "node.offline",
    "node.online",
    "client.limited",
    "client.expired",
    "tunnel.error",
    "auth.lockout",
    "security.blocked",
}


def record(
    session: Session,
    *,
    action: str,
    username: str | None = None,
    target_type: str | None = None,
    target_id: Any = None,
    level: str = "info",
    success: bool = True,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        username=username,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        level=level,
        success=success,
        ip=ip,
        user_agent=(user_agent or "")[:255] or None,
        detail=detail or {},
    )
    session.add(entry)
    if action in NOTIFY_ACTIONS:
        session.add(
            Event(
                level=level if level in {"info", "warning", "error", "critical"} else "info",
                source="panel",
                message=_describe(action, detail),
                meta={"action": action, "target_id": str(target_id) if target_id else None},
            )
        )
    log.info(
        "audit action=%s user=%s target=%s/%s success=%s",
        action,
        username,
        target_type,
        target_id,
        success,
        extra={"action": action, "user": username, "ip": ip, "target": target_id},
    )
    return entry


def _describe(action: str, detail: dict[str, Any] | None) -> str:
    detail = detail or {}
    reason = detail.get("reason") or detail.get("message")
    base = action.replace(".", " ").replace("_", " ")
    return f"{base}: {reason}" if reason else base
