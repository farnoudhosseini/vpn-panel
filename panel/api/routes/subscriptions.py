"""Subscription delivery.

Subscription URLs are unauthenticated by necessity (a VPN client cannot log
in), so each one carries an HMAC signature derived from the panel secret. The
identifiers are random and the signature is compared in constant time, which
prevents both enumeration and tampering.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import select

from ...config import get_settings
from ...core.logging import get_logger
from ...core.ratelimit import RateLimiter
from ...core.security import verify_subscription_signature
from ...db import session_scope
from ...models import Client, ClientStatus, Inbound, Node
from .clients import build_config, remote_host_for, subscription_info

log = get_logger("api.subscriptions")

router = APIRouter(prefix="/sub", tags=["subscriptions"])

subscription_limiter = RateLimiter(limit=120, window_seconds=60)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _load(sub_id: str, signature: str) -> tuple[Client, Inbound, Node | None]:
    settings = get_settings()
    if not verify_subscription_signature(sub_id, signature, settings.secret_key):
        # Do not reveal whether the id exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    with session_scope() as session:
        client = session.scalar(select(Client).where(Client.sub_id == sub_id))
        if client is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        if not client.sub_enabled:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="subscription disabled")
        if client.status in {ClientStatus.EXPIRED.value, ClientStatus.LIMITED.value}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"subscription {client.status}",
            )
        inbound = session.get(Inbound, client.inbound_id)
        node = session.get(Node, client.node_id) if inbound else None
        if inbound is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        session.expunge(client)
        session.expunge(inbound)
        if node is not None:
            session.expunge(node)
    return client, inbound, node


def _userinfo_header(client: Client) -> str:
    uploaded = int(client.up_bytes)
    downloaded = int(client.down_bytes)
    total = int(client.data_limit_bytes)
    expire = 0
    if client.expire_at:
        expires = client.expire_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        expire = int(expires.timestamp())
    return (
        f"upload={uploaded}; download={downloaded}; total={total}; "
        f"expire={expire}; multiplier={client.multiplier}"
    )


@router.get("/{sub_id}")
def subscription(
    sub_id: str,
    request: Request,
    s: str = Query(default="", max_length=64),
    format: str = Query(default="ovpn", pattern="^(ovpn|raw|base64|json)$"),
) -> Response:
    """Return the client's OpenVPN profile (or JSON metadata)."""
    allowed, retry_after = subscription_limiter.check(_client_ip(request))
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many requests",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    client, inbound, _node = _load(sub_id, s)

    if format == "json":
        payload = subscription_info(client).model_dump(mode="json")
        payload["inbound_tag"] = inbound.tag
        return Response(
            content=_dumps(payload),
            media_type="application/json",
            headers=_headers(client),
        )

    if inbound.protocol != "openvpn":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"{inbound.protocol} subscriptions are not supported yet",
        )

    with session_scope() as session:
        client = session.scalar(select(Client).where(Client.sub_id == sub_id))
        if client is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        profile = build_config(session, client)

    if format == "base64":
        import base64

        body = base64.b64encode(profile.content.encode("utf-8")).decode("ascii")
        return Response(content=body, media_type="text/plain", headers=_headers(client))

    media = "text/plain" if format == "raw" else "application/x-openvpn-profile"
    return Response(
        content=profile.content,
        media_type=media,
        headers=_headers(client, filename=profile.filename),
    )


@router.get("/{sub_id}/info")
def subscription_information(
    sub_id: str, request: Request, s: str = Query(default="", max_length=64)
) -> dict[str, Any]:
    client, inbound, node = _load(sub_id, s)
    info = subscription_info(client).model_dump(mode="json")
    info.update(
        {
            "inbound_tag": inbound.tag,
            "protocol": inbound.protocol,
            "transport": inbound.transport,
            "remote_host": remote_host_for(inbound, node),
            "remote_port": inbound.listen_port,
        }
    )
    return info


@router.get("/{sub_id}/config")
def subscription_config(
    sub_id: str, request: Request, s: str = Query(default="", max_length=64)
) -> Response:
    client, _inbound, _node = _load(sub_id, s)
    with session_scope() as session:
        fresh = session.scalar(select(Client).where(Client.sub_id == sub_id))
        if fresh is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        profile = build_config(session, fresh)
    return Response(
        content=profile.content,
        media_type="application/x-openvpn-profile",
        headers=_headers(client, filename=profile.filename),
    )


def _headers(client: Client, filename: str | None = None) -> dict[str, str]:
    headers = {
        "Subscription-Userinfo": _userinfo_header(client),
        "Profile-Title": client.name,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if filename:
        headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return headers


def _dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
