"""Client management."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import or_, select

from ...core import audit
from ...core.logging import get_logger
from ...core.security import generate_common_name, generate_sub_id, sign_subscription
from ...models import (
    Client,
    ClientCredential,
    ClientStatus,
    Inbound,
    Node,
    SessionRecord,
    utcnow,
)
from ...services import node_client, openvpn, traffic
from ..deps import Context, DbSession, OperatorUp, ViewerUp
from ..schemas import (
    ClientBulkAction,
    ClientConfigOut,
    ClientCreate,
    ClientDetail,
    ClientOut,
    ClientUpdate,
    SubscriptionInfoOut,
)

log = get_logger("api.clients")

router = APIRouter(prefix="/clients", tags=["clients"])


def _require_client(session, client_id: int) -> Client:
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return client


def _require_inbound(session, inbound_id: int) -> Inbound:
    inbound = session.get(Inbound, inbound_id)
    if inbound is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="inbound not found")
    return inbound


def refresh_inbound_on_node(session, inbound: Inbound) -> None:
    """Re-push the inbound so ccd entries and the CRL stay current."""
    from .inbounds import apply_inbound

    apply_inbound(session, inbound)


def remote_host_for(inbound: Inbound, node: Node | None) -> str:
    for candidate in (inbound.domain, inbound.sni, inbound.fake_site_domain):
        if candidate:
            return candidate
    if node is not None and node.address:
        return node.address
    from ...config import get_settings

    return get_settings().domain


def subscription_url(client: Client) -> str:
    from ...config import get_settings

    settings = get_settings()
    signature = sign_subscription(client.sub_id, settings.secret_key)
    host = settings.domain
    scheme = "https" if settings.cookie_secure else "http"
    return f"{scheme}://{host}/sub/{client.sub_id}?s={signature}"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ClientOut])
def list_clients(
    session: DbSession,
    principal: ViewerUp,
    inbound_id: int | None = Query(default=None),
    node_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status", max_length=16),
    search: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[Client]:
    query = select(Client).order_by(Client.id.desc()).limit(limit).offset(offset)
    if inbound_id is not None:
        query = query.where(Client.inbound_id == inbound_id)
    if node_id is not None:
        query = query.where(Client.node_id == node_id)
    if status_filter:
        query = query.where(Client.status == status_filter)
    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(Client.name.ilike(pattern), Client.common_name.ilike(pattern),
                Client.email.ilike(pattern), Client.sub_id.ilike(pattern))
        )
    return list(session.scalars(query))


@router.post("", response_model=ClientDetail, status_code=status.HTTP_201_CREATED)
def create_client(
    payload: ClientCreate,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
) -> ClientDetail:
    inbound = _require_inbound(session, payload.inbound_id)
    node = session.get(Node, inbound.node_id)

    common_name = payload.common_name or generate_common_name("u")
    if session.scalar(
        select(Client).where(Client.inbound_id == inbound.id, Client.common_name == common_name)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="a client with that common name exists"
        )

    expire_at = payload.expire_at
    if expire_at is not None and expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=UTC)

    client = Client(
        inbound_id=inbound.id,
        node_id=inbound.node_id,
        name=payload.name,
        email=payload.email,
        common_name=common_name,
        sub_id=generate_sub_id(),
        sub_enabled=payload.sub_enabled,
        status=ClientStatus.ACTIVE.value if payload.enabled else ClientStatus.DISABLED.value,
        enabled=payload.enabled,
        data_limit_bytes=payload.data_limit_bytes,
        multiplier=payload.multiplier,
        expire_at=expire_at,
        reset_policy=payload.reset_policy,
        reset_at=traffic.next_reset_at(payload.reset_policy),
        ip_limit=payload.ip_limit,
        device_limit=payload.device_limit,
        note=payload.note,
        billed_bytes=0.0,
    )
    session.add(client)
    session.flush()

    _, _, serial, not_after = openvpn.issue_client_certificate(session, inbound, client)
    session.flush()

    audit.record(
        session,
        action="client.create",
        username=principal.username,
        target_type="client",
        target_id=client.id,
        ip=ctx.ip,
        detail={"inbound": inbound.tag, "common_name": common_name, "multiplier": payload.multiplier},
    )

    if node is not None:
        try:
            refresh_inbound_on_node(session, inbound)
        except (HTTPException, node_client.NodeUnreachable) as exc:
            log.warning("client created but the inbound could not be refreshed: %s", exc)

    detail = _detail(session, client)
    detail.certificate_serial = serial
    detail.certificate_expires_at = not_after
    return detail


@router.get("/{client_id}", response_model=ClientDetail)
def get_client(client_id: int, session: DbSession, principal: ViewerUp) -> ClientDetail:
    return _detail(session, _require_client(session, client_id))


@router.patch("/{client_id}", response_model=ClientDetail)
def update_client(
    client_id: int,
    payload: ClientUpdate,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
) -> ClientDetail:
    client = _require_client(session, client_id)
    data = payload.model_dump(exclude_unset=True)

    changed: list[str] = []
    for field, value in data.items():
        if field == "expire_at" and value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        if field == "reset_policy" and value:
            client.reset_at = traffic.next_reset_at(value)
        if field == "enabled" and value is not None:
            client.status = (
                ClientStatus.ACTIVE.value if value else ClientStatus.DISABLED.value
            )
        setattr(client, field, value)
        changed.append(field)

    audit.record(
        session,
        action="client.update",
        username=principal.username,
        target_type="client",
        target_id=client.id,
        ip=ctx.ip,
        detail={"fields": sorted(changed)},
    )
    return _detail(session, client)


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_client(
    client_id: int,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
) -> Response:
    client = _require_client(session, client_id)
    inbound = session.get(Inbound, client.inbound_id)
    node = session.get(Node, client.node_id)
    name = client.name

    if node is not None and inbound is not None:
        try:
            node_client.disconnect(node, inbound.tag, client.common_name)
        except node_client.NodeUnreachable:
            pass

    if inbound is not None:
        openvpn.revoke_client_certificate(session, inbound, client)

    session.delete(client)
    session.flush()

    if inbound is not None:
        try:
            refresh_inbound_on_node(session, inbound)
        except (HTTPException, node_client.NodeUnreachable) as exc:
            log.warning("client deleted but the inbound could not be refreshed: %s", exc)

    audit.record(
        session,
        action="client.delete",
        username=principal.username,
        target_type="client",
        target_id=client_id,
        level="warning",
        ip=ctx.ip,
        detail={"name": name, "certificate_revoked": inbound is not None},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@router.post("/{client_id}/reset", response_model=ClientDetail)
def reset_usage(
    client_id: int,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
    keep_expiry: bool = Query(default=True),
) -> ClientDetail:
    client = _require_client(session, client_id)
    client.up_bytes = 0
    client.down_bytes = 0
    client.billed_bytes = 0.0
    if not keep_expiry:
        client.expire_at = None
    if client.status == ClientStatus.LIMITED.value and client.enabled:
        client.status = ClientStatus.ACTIVE.value
    if client.reset_policy != "never":
        client.reset_at = traffic.next_reset_at(client.reset_policy)

    audit.record(
        session,
        action="client.reset",
        username=principal.username,
        target_type="client",
        target_id=client.id,
        ip=ctx.ip,
        detail={"keep_expiry": keep_expiry},
    )
    return _detail(session, client)


@router.post("/{client_id}/disconnect")
def disconnect_client(client_id: int, session: DbSession, principal: OperatorUp) -> dict[str, Any]:
    client = _require_client(session, client_id)
    inbound = session.get(Inbound, client.inbound_id)
    node = session.get(Node, client.node_id)
    if inbound is None or node is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="inbound or node missing")
    try:
        ok = node_client.disconnect(node, inbound.tag, client.common_name)
    except node_client.NodeUnreachable as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    audit.record(
        session,
        action="client.disconnect",
        username=principal.username,
        target_type="client",
        target_id=client.id,
    )
    return {"ok": ok, "message": "session closed" if ok else "no active session found"}


@router.post("/{client_id}/rotate-keys", response_model=ClientConfigOut)
def rotate_keys(client_id: int, session: DbSession, principal: OperatorUp) -> ClientConfigOut:
    client = _require_client(session, client_id)
    inbound = _require_inbound(session, client.inbound_id)
    openvpn.issue_client_certificate(session, inbound, client)
    session.flush()
    node = session.get(Node, inbound.node_id)
    if node is not None:
        try:
            node_client.disconnect(node, inbound.tag, client.common_name)
        except node_client.NodeUnreachable:
            pass
        try:
            refresh_inbound_on_node(session, inbound)
        except (HTTPException, node_client.NodeUnreachable) as exc:
            log.warning("keys rotated but inbound refresh failed: %s", exc)
    audit.record(
        session,
        action="client.rotate_keys",
        username=principal.username,
        target_type="client",
        target_id=client.id,
        level="warning",
    )
    return build_config(session, client)


@router.get("/{client_id}/config", response_model=ClientConfigOut)
def get_config(client_id: int, session: DbSession, principal: ViewerUp) -> ClientConfigOut:
    return build_config(session, _require_client(session, client_id))


@router.get("/{client_id}/config/download")
def download_config(client_id: int, session: DbSession, principal: ViewerUp) -> Response:
    payload = build_config(session, _require_client(session, client_id))
    return Response(
        content=payload.content,
        media_type="application/x-openvpn-profile",
        headers={"Content-Disposition": f'attachment; filename="{payload.filename}"'},
    )


@router.get("/{client_id}/usage")
def client_usage(
    client_id: int,
    session: DbSession,
    principal: ViewerUp,
    hours: int = Query(default=24, ge=1, le=720),
) -> dict[str, Any]:
    client = _require_client(session, client_id)
    series = traffic.usage_series(session, client_id=client.id, hours=hours)
    return {
        "client": client.name,
        "multiplier": client.multiplier,
        "used_bytes": client.used_bytes,
        "data_limit_bytes": client.data_limit_bytes,
        "series": series,
    }


@router.get("/{client_id}/sessions")
def client_sessions(
    client_id: int,
    session: DbSession,
    principal: ViewerUp,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    client = _require_client(session, client_id)
    rows = session.scalars(
        select(SessionRecord)
        .where(SessionRecord.common_name == client.common_name)
        .order_by(SessionRecord.started_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": row.id,
            "started_at": row.started_at,
            "ended_at": row.ended_at,
            "real_address": row.real_address,
            "virtual_address": row.virtual_address,
            "bytes_received": row.bytes_received,
            "bytes_sent": row.bytes_sent,
        }
        for row in rows
    ]


@router.post("/bulk")
def bulk_action(
    payload: ClientBulkAction,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
) -> dict[str, Any]:
    clients = list(session.scalars(select(Client).where(Client.id.in_(payload.client_ids))))
    if not clients:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no matching clients")

    touched_inbounds: set[int] = set()
    affected = 0

    for client in clients:
        if payload.action == "enable":
            client.enabled = True
            if client.status == ClientStatus.DISABLED.value:
                client.status = ClientStatus.ACTIVE.value
        elif payload.action == "disable":
            client.enabled = False
            client.status = ClientStatus.DISABLED.value
            _disconnect(session, client)
        elif payload.action == "reset":
            client.up_bytes = 0
            client.down_bytes = 0
            client.billed_bytes = 0.0
            if client.status == ClientStatus.LIMITED.value and client.enabled:
                client.status = ClientStatus.ACTIVE.value
        elif payload.action == "disconnect":
            _disconnect(session, client)
        elif payload.action == "delete":
            inbound = session.get(Inbound, client.inbound_id)
            if inbound is not None:
                openvpn.revoke_client_certificate(session, inbound, client)
            session.delete(client)
        touched_inbounds.add(client.inbound_id)
        affected += 1

    session.flush()

    if payload.action in {"delete", "disable", "enable"}:
        for inbound_id in touched_inbounds:
            inbound = session.get(Inbound, inbound_id)
            if inbound is None:
                continue
            try:
                refresh_inbound_on_node(session, inbound)
            except (HTTPException, node_client.NodeUnreachable) as exc:
                log.warning("bulk action: inbound %s refresh failed: %s", inbound.tag, exc)

    audit.record(
        session,
        action=f"client.bulk_{payload.action}",
        username=principal.username,
        target_type="client",
        level="warning" if payload.action == "delete" else "info",
        ip=ctx.ip,
        detail={"count": affected},
    )
    return {"action": payload.action, "affected": affected}


@router.get("/{client_id}/subscription", response_model=SubscriptionInfoOut)
def client_subscription(
    client_id: int, session: DbSession, principal: ViewerUp
) -> SubscriptionInfoOut:
    client = _require_client(session, client_id)
    return subscription_info(client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disconnect(session, client: Client) -> None:
    inbound = session.get(Inbound, client.inbound_id)
    node = session.get(Node, client.node_id)
    if inbound is None or node is None:
        return
    try:
        node_client.disconnect(node, inbound.tag, client.common_name)
    except node_client.NodeUnreachable as exc:
        log.warning("could not disconnect %s: %s", client.common_name, exc)


def _detail(session, client: Client) -> ClientDetail:
    inbound = session.get(Inbound, client.inbound_id)
    node = session.get(Node, client.node_id)
    detail = ClientDetail.model_validate(client)
    detail.inbound_tag = inbound.tag if inbound else None
    detail.node_name = node.name if node else None
    detail.subscription_url = subscription_url(client) if client.sub_enabled else None

    credential = session.scalar(
        select(ClientCredential).where(
            ClientCredential.client_id == client.id, ClientCredential.kind == "cert"
        )
    )
    if credential is not None:
        detail.certificate_serial = credential.serial
        detail.certificate_expires_at = credential.expires_at
        detail.certificate_revoked = credential.revoked

    if inbound is not None and node is not None:
        try:
            sessions = node_client.fetch_sessions(
                node, inbound.tag, timeout=node_client.STATUS_TIMEOUT
            )
        except node_client.NodeUnreachable:
            sessions = []
        for entry in sessions:
            if entry.get("common_name") == client.common_name:
                detail.online = True
                detail.real_address = entry.get("real_address")
                detail.virtual_address = entry.get("virtual_address")
                break
    return detail


def build_config(session, client: Client) -> ClientConfigOut:
    inbound = session.get(Inbound, client.inbound_id)
    if inbound is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="inbound is missing")
    node = session.get(Node, inbound.node_id)

    credentials = openvpn.client_credentials(session, client.id)
    if not credentials.get("cert") or not credentials.get("key"):
        openvpn.issue_client_certificate(session, inbound, client)
        session.flush()
        credentials = openvpn.client_credentials(session, client.id)

    material = openvpn.ensure_material(session, inbound)
    host = remote_host_for(inbound, node)
    content = openvpn.render_client_config(
        inbound,
        client,
        credentials=credentials,
        material=material,
        remote_host=host,
    )
    return ClientConfigOut(
        filename=f"{client.common_name}.ovpn",
        content=content,
        remote_host=host,
        remote_port=inbound.listen_port,
        protocol=inbound.transport,
    )


def subscription_info(client: Client) -> SubscriptionInfoOut:
    now = utcnow()
    days_remaining = None
    if client.expire_at:
        expires = client.expire_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        days_remaining = max(0, (expires - now).days)
    return SubscriptionInfoOut(
        client_id=client.id,
        name=client.name,
        status=client.status,
        sub_id=client.sub_id,
        sub_enabled=client.sub_enabled,
        # Billed usage is what counts against the quota, so report that.
        used_bytes=client.used_bytes,
        data_limit_bytes=client.data_limit_bytes,
        remaining_bytes=client.remaining_bytes,
        expire_at=client.expire_at,
        days_remaining=days_remaining,
        subscription_url=subscription_url(client) if client.sub_enabled else None,
    )
