"""Tunnel management and the blueprint editor's server side.

Static routes are declared before ``/{tunnel_id}`` ones so paths such as
``/tunnels/backpack/status`` are not captured as a tunnel identifier.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select

from ...core import audit
from ...core.logging import get_logger
from ...models import Node, Tunnel, TunnelStatus, utcnow
from ...services import node_client, tunnels
from ...services.plan import service_state
from ..deps import Context, DbSession, OperatorUp, ViewerUp
from ..schemas import (
    TransportOut,
    TunnelApplyResult,
    TunnelCreate,
    TunnelDetail,
    TunnelOut,
    TunnelPlanOut,
    TunnelUpdate,
)

log = get_logger("api.tunnels")

router = APIRouter(prefix="/tunnels", tags=["tunnels"])

ACTIVE_STATES = {"active", "True", "ok"}


def _require_tunnel(session, tunnel_id: int) -> Tunnel:
    tunnel = session.get(Tunnel, tunnel_id)
    if tunnel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tunnel not found")
    return tunnel


def _require_node(session, node_id: int) -> Node:
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    return node


def _service_name(tunnel: Tunnel) -> str:
    if tunnels.get_transport(tunnel.transport).engine == "backpack":
        return tunnels.backpack_unit_name(tunnel.name)
    return tunnels.unit_name(tunnel)


def _detail(session, tunnel: Tunnel) -> TunnelDetail:
    local = session.get(Node, tunnel.local_node_id)
    remote = session.get(Node, tunnel.remote_node_id)
    service = _service_name(tunnel)

    detail = TunnelDetail.model_validate(tunnel)
    detail.local_node_name = local.name if local else None
    detail.remote_node_name = remote.name if remote else None
    detail.local_service_state = _node_service_state(local, service)
    detail.remote_service_state = _node_service_state(remote, service)
    detail.notes = _plan_notes(session, tunnel, local, remote)
    return detail


def _node_service_state(node: Node | None, service: str) -> str | None:
    if node is None:
        return None
    if node.is_local:
        _, state = service_state(service)
        return state
    try:
        result = node_client.call(
            node, "service.state", {"unit": service}, timeout=node_client.STATUS_TIMEOUT
        )
    except (node_client.NodeUnreachable, ValueError) as exc:
        return f"unreachable: {exc}"
    return str(result.get("state") or "unknown")


def _plan_notes(session, tunnel: Tunnel, local: Node | None, remote: Node | None) -> list[str]:
    if local is None or remote is None:
        return ["one of the endpoints no longer exists"]
    try:
        return tunnels.plan_tunnel(tunnel, local, remote).notes
    except tunnels.TunnelError as exc:
        return [str(exc)]


# ---------------------------------------------------------------------------
# Catalogue (static path)
# ---------------------------------------------------------------------------


@router.get("/transports", response_model=list[TransportOut])
def list_transports(principal: ViewerUp) -> list[TransportOut]:
    return [TransportOut(**item) for item in tunnels.transport_catalogue()]


@router.get("/backpack/status")
def backpack_status(
    session: DbSession,
    principal: ViewerUp,
    node_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """BackPack version and tunnels, read through the CLI on one node."""
    if node_id is None:
        return tunnels.backpack_info()
    node = _require_node(session, node_id)
    try:
        return node_client.call(
            node, "backpack.info", timeout=node_client.STATUS_TIMEOUT
        )
    except node_client.NodeUnreachable as exc:
        return {"installed": False, "version": None, "tunnels": [], "error": str(exc)}


@router.post("/backpack/link")
def backpack_apply_link(
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
    link: str = Query(min_length=12, max_length=8192),
    node_id: int | None = Query(default=None),
    name: str | None = Query(default=None, max_length=80),
    host: str | None = Query(default=None, max_length=255),
) -> dict[str, Any]:
    """Hand a BackPack setup link to the BackPack CLI on the local node.

    Setup links are produced by BackPack itself; Panah validates and forwards
    them rather than inventing a format of its own.
    """
    status_info = tunnels.link_status(link)
    if not status_info.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=status_info.get("error")
        )

    if node_id is not None:
        node = _require_node(session, node_id)
        if not node.is_local:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="apply setup links on the node that runs BackPack",
            )

    ok, message = tunnels.backpack_link_apply(link, name=name, host=host)
    audit.record(
        session,
        action="tunnel.backpack_link",
        username=principal.username,
        target_type="tunnel",
        success=ok,
        ip=ctx.ip,
        detail={"version": status_info.get("version")},
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message)
    return {"ok": True, "message": message, "version": status_info.get("version")}


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=list[TunnelOut])
def list_tunnels(
    session: DbSession,
    principal: ViewerUp,
    node_id: int | None = Query(default=None),
) -> list[Tunnel]:
    query = select(Tunnel).order_by(Tunnel.id.desc())
    if node_id is not None:
        query = query.where(
            (Tunnel.local_node_id == node_id) | (Tunnel.remote_node_id == node_id)
        )
    return list(session.scalars(query))


@router.post("", response_model=TunnelDetail, status_code=status.HTTP_201_CREATED)
def create_tunnel(
    payload: TunnelCreate,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
    apply: bool = Query(default=False, description="deploy immediately after creating"),
) -> TunnelDetail:
    if payload.local_node_id == payload.remote_node_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a tunnel needs two different nodes",
        )
    try:
        spec = tunnels.get_transport(payload.transport)
    except tunnels.TunnelError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    _require_node(session, payload.local_node_id)
    _require_node(session, payload.remote_node_id)

    if session.scalar(select(Tunnel).where(Tunnel.name == payload.name)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="tunnel name already exists"
        )

    tunnel = Tunnel(
        name=payload.name,
        engine=spec.engine,
        transport=payload.transport,
        mode=payload.mode,
        local_node_id=payload.local_node_id,
        remote_node_id=payload.remote_node_id,
        listen_port=payload.listen_port,
        interface_name=payload.interface_name,
        mtu=payload.mtu,
        forwarded_ports=[item.model_dump() for item in payload.forwarded_ports],
        options=payload.options,
        enabled=payload.enabled,
    )
    session.add(tunnel)
    session.flush()
    tunnels.ensure_token(tunnel)

    audit.record(
        session,
        action="tunnel.create",
        username=principal.username,
        target_type="tunnel",
        target_id=tunnel.id,
        ip=ctx.ip,
        detail={
            "transport": payload.transport,
            "engine": spec.engine,
            "mode": payload.mode,
            "port": payload.listen_port,
        },
    )

    if apply:
        result = _deploy(session, tunnel)
        audit.record(
            session,
            action="tunnel.apply",
            username=principal.username,
            target_type="tunnel",
            target_id=tunnel.id,
            ip=ctx.ip,
            success=result.ok,
            detail={"message": result.message},
        )
    return _detail(session, tunnel)


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------


def _deploy(session, tunnel: Tunnel) -> TunnelApplyResult:
    local = _require_node(session, tunnel.local_node_id)
    remote = _require_node(session, tunnel.remote_node_id)

    if local.id == remote.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="a tunnel needs two different nodes"
        )

    try:
        plan = tunnels.plan_tunnel(tunnel, local, remote)
    except tunnels.TunnelError as exc:
        tunnel.status = TunnelStatus.ERROR.value
        tunnel.last_error = str(exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    local_report: dict[str, Any] | None = None
    remote_report: dict[str, Any] | None = None
    try:
        local_report = node_client.apply_side(session, local, plan.local)
        remote_report = node_client.apply_side(session, remote, plan.remote)
    except node_client.NodeUnreachable as exc:
        tunnel.status = TunnelStatus.ERROR.value
        tunnel.last_error = str(exc)
        return TunnelApplyResult(
            ok=False,
            message=str(exc),
            local=local_report,
            remote=remote_report,
            notes=plan.notes,
        )

    ok = bool(local_report.get("ok")) and bool(remote_report.get("ok"))
    tunnel.status = TunnelStatus.UP.value if ok else TunnelStatus.ERROR.value
    tunnel.last_error = None if ok else "one or both ends reported a failure"
    if ok:
        tunnel.last_handshake_at = utcnow()
    return TunnelApplyResult(
        ok=ok,
        message="tunnel is up" if ok else "the tunnel was deployed with errors",
        local=local_report,
        remote=remote_report,
        notes=plan.notes,
    )


def _teardown(session, tunnel: Tunnel) -> TunnelApplyResult:
    payload = {
        "id": tunnel.id,
        "name": tunnel.name,
        "engine": tunnel.engine,
        "transport": tunnel.transport,
        "mode": tunnel.mode,
        "listen_port": tunnel.listen_port,
        "interface_name": tunnel.interface_name,
    }
    reports: dict[str, Any] = {}
    errors: list[str] = []
    for label in ("local_node_id", "remote_node_id"):
        node = session.get(Node, getattr(tunnel, label))
        if node is None:
            continue
        try:
            reports[label] = node_client.teardown_tunnel(
                node, payload, timeout=node_client.TEARDOWN_TIMEOUT
            )
        except node_client.NodeUnreachable as exc:
            # The record is removed either way: an operator must be able to
            # forget a tunnel whose endpoint no longer exists.
            errors.append(f"{node.name}: {exc}")
    tunnel.status = TunnelStatus.DOWN.value
    return TunnelApplyResult(
        ok=not errors,
        message="; ".join(errors) if errors else "tunnel torn down",
        local=reports.get("local_node_id"),
        remote=reports.get("remote_node_id"),
    )


# ---------------------------------------------------------------------------
# Single tunnel
# ---------------------------------------------------------------------------


@router.get("/{tunnel_id}", response_model=TunnelDetail)
def get_tunnel(tunnel_id: int, session: DbSession, principal: ViewerUp) -> TunnelDetail:
    return _detail(session, _require_tunnel(session, tunnel_id))


@router.patch("/{tunnel_id}", response_model=TunnelDetail)
def update_tunnel(
    tunnel_id: int,
    payload: TunnelUpdate,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
) -> TunnelDetail:
    tunnel = _require_tunnel(session, tunnel_id)
    data = payload.model_dump(exclude_unset=True)

    if "transport" in data and data["transport"] != tunnel.transport:
        try:
            spec = tunnels.get_transport(data["transport"])
        except tunnels.TunnelError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        tunnel.transport = data["transport"]
        tunnel.engine = spec.engine
        # A different engine needs different material (keys, interfaces).
        tunnel.interface_name = None

    if payload.forwarded_ports is not None:
        tunnel.forwarded_ports = [item.model_dump() for item in payload.forwarded_ports]

    if payload.options is not None:
        # Merge so generated material such as WireGuard keys survives an edit.
        merged = dict(tunnel.options or {})
        merged.update(payload.options)
        tunnel.options = merged

    for field in ("name", "mode", "listen_port", "interface_name", "mtu", "enabled"):
        if field in data and data[field] is not None:
            setattr(tunnel, field, data[field])

    audit.record(
        session,
        action="tunnel.update",
        username=principal.username,
        target_type="tunnel",
        target_id=tunnel.id,
        ip=ctx.ip,
        detail={"fields": sorted(data)},
    )
    return _detail(session, tunnel)


@router.delete("/{tunnel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tunnel(
    tunnel_id: int,
    session: DbSession,
    principal: OperatorUp,
    ctx: Context,
    teardown: bool = Query(default=True),
) -> Response:
    tunnel = _require_tunnel(session, tunnel_id)
    name = tunnel.name
    if teardown:
        _teardown(session, tunnel)
    session.delete(tunnel)
    audit.record(
        session,
        action="tunnel.delete",
        username=principal.username,
        target_type="tunnel",
        target_id=tunnel_id,
        level="warning",
        ip=ctx.ip,
        detail={"name": name, "torn_down": teardown},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{tunnel_id}/plan", response_model=TunnelPlanOut)
def preview_plan(tunnel_id: int, session: DbSession, principal: ViewerUp) -> TunnelPlanOut:
    tunnel = _require_tunnel(session, tunnel_id)
    local = _require_node(session, tunnel.local_node_id)
    remote = _require_node(session, tunnel.remote_node_id)
    try:
        plan = tunnels.plan_tunnel(tunnel, local, remote)
    except tunnels.TunnelError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    session.flush()
    return TunnelPlanOut(
        engine=plan.engine,
        transport=plan.transport,
        mode=plan.mode,
        notes=plan.notes,
        local=plan.local.to_dict(),
        remote=plan.remote.to_dict(),
    )


@router.post("/{tunnel_id}/apply", response_model=TunnelApplyResult)
def apply_tunnel(
    tunnel_id: int, session: DbSession, principal: OperatorUp, ctx: Context
) -> TunnelApplyResult:
    tunnel = _require_tunnel(session, tunnel_id)
    result = _deploy(session, tunnel)
    audit.record(
        session,
        action="tunnel.apply",
        username=principal.username,
        target_type="tunnel",
        target_id=tunnel.id,
        ip=ctx.ip,
        success=result.ok,
        detail={"message": result.message},
    )
    return result


@router.post("/{tunnel_id}/stop", response_model=TunnelApplyResult)
def stop_tunnel(tunnel_id: int, session: DbSession, principal: OperatorUp) -> TunnelApplyResult:
    tunnel = _require_tunnel(session, tunnel_id)
    result = _teardown(session, tunnel)
    audit.record(
        session,
        action="tunnel.stop",
        username=principal.username,
        target_type="tunnel",
        target_id=tunnel.id,
    )
    return result


@router.post("/{tunnel_id}/status", response_model=TunnelDetail)
def refresh_status(tunnel_id: int, session: DbSession, principal: OperatorUp) -> TunnelDetail:
    tunnel = _require_tunnel(session, tunnel_id)
    service = _service_name(tunnel)
    states = [
        _node_service_state(session.get(Node, tunnel.local_node_id), service),
        _node_service_state(session.get(Node, tunnel.remote_node_id), service),
    ]
    known = [state for state in states if state and not state.startswith("unreachable")]
    if known and all(state in ACTIVE_STATES for state in known):
        tunnel.status = TunnelStatus.UP.value
        tunnel.last_handshake_at = utcnow()
        tunnel.last_error = None
    elif known and all(state in {"inactive", "failed", "dead"} for state in known):
        tunnel.status = TunnelStatus.DOWN.value
    return _detail(session, tunnel)


@router.get("/{tunnel_id}/artifacts")
def tunnel_artifacts(tunnel_id: int, session: DbSession, principal: ViewerUp) -> dict[str, Any]:
    """Where the generated files live on each end."""
    tunnel = _require_tunnel(session, tunnel_id)
    local = _require_node(session, tunnel.local_node_id)
    remote = _require_node(session, tunnel.remote_node_id)
    plan = tunnels.plan_tunnel(tunnel, local, remote)
    return {
        "service": plan.local.service,
        "local": {
            "node": plan.local.node_name,
            "files": [item.path for item in plan.local.files],
            "units": [item.name for item in plan.local.units],
        },
        "remote": {
            "node": plan.remote.node_name,
            "files": [item.path for item in plan.remote.files],
            "units": [item.name for item in plan.remote.units],
        },
    }
