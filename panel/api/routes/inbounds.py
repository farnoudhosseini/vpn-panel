"""Inbound management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select

from ...core import audit
from ...core.logging import get_logger
from ...models import Client, Inbound, Node, PkiMaterial
from ...services import node_client, node_ops, openvpn
from ...services import plan as plan_mod
from ..deps import AdminOnly, Context, DbSession, OperatorUp, ViewerUp
from ..schemas import (
    InboundApplyResult,
    InboundCreate,
    InboundDetail,
    InboundOut,
    InboundUpdate,
)

log = get_logger("api.inbounds")

router = APIRouter(prefix="/inbounds", tags=["inbounds"])

SUPPORTED_PROTOCOLS = [
    {
        "key": "openvpn",
        "label": "OpenVPN",
        "transports": ["udp", "tcp"],
        "status": "stable",
        "notes": "TLS with tls-crypt, per-client certificates and CRL revocation.",
    }
]


def _require_inbound(session, inbound_id: int) -> Inbound:
    inbound = session.get(Inbound, inbound_id)
    if inbound is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="inbound not found")
    return inbound


def _require_node(session, node_id: int) -> Node:
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    if not node.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="node is disabled")
    return node


def inbound_payload(session, inbound: Inbound) -> dict[str, Any]:
    """Everything the target node needs to run this inbound."""
    material = openvpn.ensure_material(session, inbound)
    ccd = openvpn.build_ccd_map(session, inbound)
    return node_ops.inbound_payload(inbound, material, ccd)


def apply_inbound(session, inbound: Inbound) -> dict[str, Any]:
    node = _require_node(session, inbound.node_id)
    payload = inbound_payload(session, inbound)
    side: plan_mod.SidePlan = node_ops.plan_inbound(payload)
    result = node_client.apply_side(session, node, side)
    if not result.get("ok", False):
        failed = [
            entry
            for entry in (*result.get("files", []), *result.get("units", []))
            if not entry.get("ok")
        ]
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"message": "the node rejected part of the configuration", "report": result,
                    "failed": failed},
        )
    return result


def sync_nginx(session, node: Node, port: int | None, *, fake_title: str | None = None) -> dict[str, Any]:
    """Rebuild the shared-port router for every inbound listening on ``port``."""
    group = session.scalars(
        select(Inbound).where(
            Inbound.node_id == node.id,
            Inbound.nginx_enabled.is_(True),
            Inbound.nginx_port == port,
            Inbound.enabled.is_(True),
        )
    ).all()
    if not group:
        return {"ok": True, "skipped": "no inbounds use this front end"}

    title = fake_title or next(
        (item.fake_site_title for item in group if item.fake_site_title), "Cloud Gateway"
    )
    payload: dict[str, Any] = {
        "name": f"share-{port}",
        "fake_site_title": title,
        "domain": next((item.fake_site_domain for item in group if item.fake_site_domain), "_"),
        "http_ports": [8080],
        "stream_router": {
            "name": f"share-{port}",
            "listen_port": port,
            "fallback_host": "127.0.0.1",
            "fallback_port": 8080,
            "routes": [
                {
                    "server_name": item.sni or item.domain,
                    "name": f"inbound-{item.id}",
                    "host": "127.0.0.1",
                    "port": item.listen_port,
                }
                for item in group
                if (item.sni or item.domain)
            ],
        },
    }
    return node_client.apply_nginx(node, payload)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("/protocols")
def protocols(principal: ViewerUp) -> dict[str, Any]:
    return {"protocols": SUPPORTED_PROTOCOLS}


@router.get("", response_model=list[InboundOut])
def list_inbounds(
    session: DbSession,
    principal: ViewerUp,
    node_id: int | None = Query(default=None),
    enabled: bool | None = Query(default=None),
) -> list[Inbound]:
    query = select(Inbound).order_by(Inbound.node_id, Inbound.tag)
    if node_id is not None:
        query = query.where(Inbound.node_id == node_id)
    if enabled is not None:
        query = query.where(Inbound.enabled.is_(enabled))
    return list(session.scalars(query))


@router.post("", response_model=InboundDetail, status_code=status.HTTP_201_CREATED)
def create_inbound(
    payload: InboundCreate,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
) -> InboundDetail:
    node = _require_node(session, payload.node_id)

    if session.scalar(select(Inbound).where(Inbound.tag == payload.tag)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="inbound tag already exists")
    clash = session.scalar(
        select(Inbound).where(
            Inbound.node_id == payload.node_id,
            Inbound.listen_port == payload.listen_port,
            Inbound.protocol == payload.protocol,
        )
    )
    if clash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"port {payload.listen_port} is already used by inbound {clash.tag}",
        )

    try:
        settings = openvpn.validate_settings(payload.settings.model_dump(exclude_none=True))
    except openvpn.OpenVPNError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    inbound = Inbound(
        node_id=payload.node_id,
        tag=payload.tag,
        protocol=payload.protocol,
        transport=payload.transport,
        listen_port=payload.listen_port,
        listen_host=payload.listen_host,
        network_cidr=payload.network_cidr,
        domain=payload.domain,
        enabled=payload.enabled,
        settings=settings,
        nginx_enabled=payload.nginx_enabled,
        nginx_port=payload.nginx_port,
        sni=payload.sni,
        fake_site_domain=payload.fake_site_domain,
        fake_site_title=payload.fake_site_title,
    )
    session.add(inbound)
    session.flush()

    audit.record(
        session,
        action="inbound.create",
        username=principal.username,
        target_type="inbound",
        target_id=inbound.id,
        ip=ctx.ip,
        detail={"tag": inbound.tag, "node": node.name, "port": inbound.listen_port},
    )

    try:
        apply_inbound(session, inbound)
    except HTTPException as exc:
        session.rollback()
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": "inbound was not created because the node rejected it",
                    "report": exc.detail},
        ) from exc

    if inbound.nginx_enabled and inbound.nginx_port:
        sync_nginx(session, node, inbound.nginx_port)
    return get_inbound(inbound.id, session, principal)


@router.get("/{inbound_id}", response_model=InboundDetail)
def get_inbound(inbound_id: int, session: DbSession, principal: ViewerUp) -> InboundDetail:
    inbound = _require_inbound(session, inbound_id)
    node = session.get(Node, inbound.node_id)
    detail = InboundDetail.model_validate(inbound)
    detail.node_name = node.name if node else None
    detail.client_count = int(
        session.scalar(select(func.count(Client.id)).where(Client.inbound_id == inbound.id)) or 0
    )
    detail.has_pki = bool(
        session.scalar(
            select(func.count(PkiMaterial.id)).where(PkiMaterial.inbound_id == inbound.id)
        )
    )
    detail.effective_settings = openvpn.effective_settings(inbound)

    if node is not None:
        unit = openvpn.unit_name(inbound.tag)
        if node.is_local:
            from ...services.plan import service_state

            active, state = service_state(unit)
            detail.service_active = active
            detail.service_state = state
        else:
            state = (inbound.settings or {}).get("_service_state")
            detail.service_state = state or "unknown"
    return detail


@router.patch("/{inbound_id}", response_model=InboundDetail)
def update_inbound(
    inbound_id: int,
    payload: InboundUpdate,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
) -> InboundDetail:
    inbound = _require_inbound(session, inbound_id)
    node = _require_node(session, inbound.node_id)

    data = payload.model_dump(exclude_unset=True)
    settings_patch = data.pop("settings", None)

    if "listen_port" in data and data["listen_port"] != inbound.listen_port:
        clash = session.scalar(
            select(Inbound).where(
                Inbound.node_id == inbound.node_id,
                Inbound.listen_port == data["listen_port"],
                Inbound.protocol == inbound.protocol,
                Inbound.id != inbound.id,
            )
        )
        if clash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"port {data['listen_port']} is already used by {clash.tag}",
            )

    if settings_patch is not None:
        merged = {**(inbound.settings or {}), **{k: v for k, v in settings_patch.items() if v is not None}}
        try:
            inbound.settings = openvpn.validate_settings(merged)
        except openvpn.OpenVPNError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    for field in (
        "transport",
        "listen_port",
        "listen_host",
        "network_cidr",
        "domain",
        "enabled",
        "nginx_enabled",
        "nginx_port",
        "sni",
        "fake_site_domain",
        "fake_site_title",
    ):
        if field in data and data[field] is not None:
            setattr(inbound, field, data[field])

    audit.record(
        session,
        action="inbound.update",
        username=principal.username,
        target_type="inbound",
        target_id=inbound.id,
        ip=ctx.ip,
        detail={"fields": sorted(data) + (["settings"] if settings_patch else [])},
    )

    apply_inbound(session, inbound)
    if inbound.nginx_enabled and inbound.nginx_port:
        sync_nginx(session, node, inbound.nginx_port)
    elif not inbound.nginx_enabled and inbound.nginx_port:
        try:
            node_client.call(node, "nginx.remove", {"name": f"share-{inbound.nginx_port}"})
        except node_client.NodeUnreachable as exc:
            log.warning("could not clear the nginx front end for %s: %s", inbound.tag, exc)
    return get_inbound(inbound.id, session, principal)


@router.delete("/{inbound_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_inbound(
    inbound_id: int,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
    purge_clients: bool = Query(default=True),
) -> Response:
    inbound = _require_inbound(session, inbound_id)
    node = session.get(Node, inbound.node_id)
    tag = inbound.tag
    port = inbound.nginx_port

    if node is not None:
        try:
            node_client.call(
                node,
                "inbound.remove",
                {"inbound": {"id": inbound.id, "tag": tag, "node_id": inbound.node_id,
                             "listen_port": inbound.listen_port}},
            )
        except node_client.NodeUnreachable as exc:
            log.warning("could not remove inbound %s from node: %s", tag, exc)

    if purge_clients:
        for client in session.scalars(select(Client).where(Client.inbound_id == inbound.id)):
            session.delete(client)

    session.delete(inbound)
    session.flush()

    if node is not None and port:
        try:
            sync_nginx(session, node, port)
        except node_client.NodeUnreachable:
            pass

    audit.record(
        session,
        action="inbound.delete",
        username=principal.username,
        target_type="inbound",
        target_id=inbound_id,
        level="warning",
        ip=ctx.ip,
        detail={"tag": tag, "purged_clients": purge_clients},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@router.post("/{inbound_id}/apply", response_model=InboundApplyResult)
def apply_now(inbound_id: int, session: DbSession, principal: OperatorUp) -> InboundApplyResult:
    inbound = _require_inbound(session, inbound_id)
    result = apply_inbound(session, inbound)
    return InboundApplyResult(ok=True, message="configuration applied", steps=[result])


@router.post("/{inbound_id}/restart", response_model=InboundApplyResult)
def restart_inbound(inbound_id: int, session: DbSession, principal: OperatorUp) -> InboundApplyResult:
    inbound = _require_inbound(session, inbound_id)
    node = _require_node(session, inbound.node_id)
    unit = openvpn.unit_name(inbound.tag)
    result = node_client.call(node, "plan.apply", {
        "plan": plan_mod.SidePlan(
            role="inbound",
            node_id=node.id,
            node_name=inbound.tag,
            commands=[plan_mod.CommandSpec(["systemctl", "restart", unit])],
            service=unit,
        ).to_dict()
    })
    return InboundApplyResult(
        ok=bool(result.get("ok")), message="inbound restarted"
    )


@router.post("/{inbound_id}/stop", response_model=InboundApplyResult)
def stop_inbound(inbound_id: int, session: DbSession, principal: OperatorUp) -> InboundApplyResult:
    inbound = _require_inbound(session, inbound_id)
    _require_node(session, inbound.node_id)
    inbound.enabled = False
    apply_inbound(session, inbound)
    audit.record(
        session,
        action="inbound.stop",
        username=principal.username,
        target_type="inbound",
        target_id=inbound.id,
    )
    return InboundApplyResult(ok=True, message="inbound stopped")


@router.post("/{inbound_id}/rotate-pki", response_model=InboundApplyResult)
def rotate_pki(inbound_id: int, session: DbSession, principal: AdminOnly) -> InboundApplyResult:
    inbound = _require_inbound(session, inbound_id)
    openvpn.ensure_material(session, inbound, rotate=True)
    session.flush()
    result = apply_inbound(session, inbound)
    audit.record(
        session,
        action="inbound.rotate_pki",
        username=principal.username,
        target_type="inbound",
        target_id=inbound.id,
        level="warning",
    )
    return InboundApplyResult(
        ok=True,
        message="new CA and server certificate issued; existing clients must be reissued",
        steps=[result],
    )


@router.get("/{inbound_id}/server-config")
def server_config(inbound_id: int, session: DbSession, principal: ViewerUp) -> dict[str, str]:
    inbound = _require_inbound(session, inbound_id)
    return {"config": openvpn.render_server_config(inbound)}


@router.get("/{inbound_id}/status")
def inbound_status(inbound_id: int, session: DbSession, principal: ViewerUp) -> dict[str, Any]:
    inbound = _require_inbound(session, inbound_id)
    node = session.get(Node, inbound.node_id)
    if node is None:
        return {"tag": inbound.tag, "sessions": [], "error": "node missing"}
    try:
        sessions = node_client.fetch_sessions(
            node, inbound.tag, timeout=node_client.STATUS_TIMEOUT
        )
    except node_client.NodeUnreachable as exc:
        return {"tag": inbound.tag, "sessions": [], "error": str(exc)}
    return {
        "tag": inbound.tag,
        "sessions": sessions,
        "counts": openvpn.summarize(
            [
                openvpn.Session(
                    common_name=str(item.get("common_name", "")),
                    bytes_received=int(item.get("bytes_received") or 0),
                    bytes_sent=int(item.get("bytes_sent") or 0),
                )
                for item in sessions
            ]
        ),
    }


@router.post("/{inbound_id}/nginx", response_model=InboundApplyResult)
def apply_nginx_front(
    inbound_id: int, session: DbSession, principal: OperatorUp
) -> InboundApplyResult:
    inbound = _require_inbound(session, inbound_id)
    node = _require_node(session, inbound.node_id)
    if not inbound.nginx_enabled or not inbound.nginx_port:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="enable the nginx front end and pick a shared port first",
        )
    try:
        result = sync_nginx(session, node, inbound.nginx_port, fake_title=inbound.fake_site_title)
    except node_client.NodeUnreachable as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    audit.record(
        session,
        action="inbound.nginx",
        username=principal.username,
        target_type="inbound",
        target_id=inbound.id,
        detail={"port": inbound.nginx_port},
    )
    return InboundApplyResult(
        ok=bool(result.get("ok")), message="shared-port front end applied", steps=[result]
    )
