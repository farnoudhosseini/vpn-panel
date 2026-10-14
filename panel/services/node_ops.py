"""Operations a node can perform.

The panel and the node agent call the *same* functions. For a local node the
panel calls them in-process; for a remote node it ships the rendered plan over
HTTPS and the agent calls :func:`apply_plan`. Keeping one implementation means
an inbound behaves identically wherever it runs.
"""

from __future__ import annotations

import platform
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from ..core.logging import get_logger
from ..models import Inbound
from . import nginx, openvpn, paths, shell, tunnels
from . import plan as plan_mod

log = get_logger("node_ops")


@dataclass(slots=True)
class Capability:
    key: str
    installed: bool
    version: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "installed": self.installed,
            "version": self.version,
            "detail": self.detail,
        }


def capabilities() -> dict[str, Any]:
    """Everything the panel needs to know before offering a feature."""
    openvpn_caps = openvpn.detect()
    nginx_caps = nginx.capabilities()
    backpack = tunnels.backpack_available()

    items = [
        Capability("openvpn", openvpn_caps.installed, openvpn_caps.version, openvpn_caps.detail),
        Capability("nginx", nginx_caps["installed"], nginx_caps.get("version"), nginx_caps.get("detail", "")),
        Capability("backpack", backpack, None, "" if backpack else "binary not installed"),
        Capability("wireguard", shell.available("wg") or shell.available("wg-quick")),
        Capability("iptables", shell.available("iptables")),
        Capability("certbot", shell.available("certbot")),
        Capability("systemd", shell.available("systemctl")),
    ]

    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "is_root": shell.is_root(),
        "capabilities": [item.as_dict() for item in items],
        "ports": _listening_ports(),
    }


def metrics() -> dict[str, Any]:
    """Cheap host snapshot for the dashboard."""
    memory = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage("/")
        disk_percent = disk.percent
    except OSError:  # pragma: no cover
        disk_percent = 0.0
    net = psutil.net_io_counters()
    try:
        load1, load5, load15 = psutil.getloadavg()
    except (AttributeError, OSError):  # pragma: no cover
        load1 = load5 = load15 = 0.0

    return {
        "ts": datetime.now(UTC).isoformat(),
        "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
        "cpu_count": psutil.cpu_count(logical=True) or 1,
        "memory_percent": round(memory.percent, 1),
        "memory_total": memory.total,
        "memory_used": memory.used,
        "disk_percent": round(disk_percent, 1),
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "uptime_seconds": int(_uptime()),
        "net_rx_bytes": net.bytes_recv,
        "net_tx_bytes": net.bytes_sent,
        "connections": _connection_count(),
    }


def _uptime() -> float:
    try:
        return datetime.now(UTC).timestamp() - psutil.boot_time()
    except Exception:  # pragma: no cover
        return 0.0


def _connection_count() -> int:
    try:
        return sum(1 for c in psutil.net_connections(kind="tcp") if c.status == "ESTABLISHED")
    except (psutil.AccessDenied, OSError):  # pragma: no cover
        return 0


def _listening_ports() -> list[int]:
    ports: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr:
                ports.add(int(conn.laddr.port))
    except (psutil.AccessDenied, OSError):  # pragma: no cover
        pass
    return sorted(ports)


# ---------------------------------------------------------------------------
# Inbounds
# ---------------------------------------------------------------------------


def inbound_from_payload(data: Mapping[str, Any]) -> Inbound:
    """Rebuild a transient inbound from a plan payload."""
    return Inbound(
        id=int(data.get("id", 0)),
        node_id=int(data.get("node_id", 0)),
        tag=str(data["tag"]),
        protocol=str(data.get("protocol", "openvpn")),
        transport=str(data.get("transport", "udp")),
        listen_port=int(data["listen_port"]),
        network_cidr=str(data.get("network_cidr", "10.8.0.0/24")),
        domain=data.get("domain"),
        listen_host=str(data.get("listen_host", "0.0.0.0")),
        sni=data.get("sni"),
        settings=dict(data.get("settings") or {}),
        enabled=bool(data.get("enabled", True)),
    )


def material_from_payload(data: Mapping[str, Any]) -> openvpn.Material:
    return openvpn.Material(
        ca_cert=str(data.get("ca_cert", "")),
        ca_key=str(data.get("ca_key", "")),
        server_cert=str(data.get("server_cert", "")),
        server_key=str(data.get("server_key", "")),
        tls_crypt=str(data.get("tls_crypt", "")),
        crl=str(data.get("crl", "")),
    )


def inbound_payload(inbound: Inbound, material: openvpn.Material, ccd: Mapping[str, str]) -> dict[str, Any]:
    return {
        "inbound": {
            "id": inbound.id,
            "node_id": inbound.node_id,
            "tag": inbound.tag,
            "protocol": inbound.protocol,
            "transport": inbound.transport,
            "listen_port": inbound.listen_port,
            "network_cidr": inbound.network_cidr,
            "domain": inbound.domain,
            "listen_host": inbound.listen_host,
            "sni": inbound.sni,
            "settings": inbound.settings or {},
            "enabled": inbound.enabled,
        },
        "material": {
            "ca_cert": material.ca_cert,
            "ca_key": material.ca_key,
            "server_cert": material.server_cert,
            "server_key": material.server_key,
            "tls_crypt": material.tls_crypt,
            "crl": material.crl,
        },
        "ccd": dict(ccd),
    }


def plan_inbound(payload: Mapping[str, Any]) -> plan_mod.SidePlan:
    """Render the files/units needed to run one inbound on the target host."""
    inbound = inbound_from_payload(payload["inbound"])
    material = material_from_payload(payload.get("material") or {})
    ccd: Mapping[str, str] = payload.get("ccd") or {}
    directory = openvpn.inbound_dir(inbound)
    caps = openvpn.detect()
    binary = caps.binary or "/usr/sbin/openvpn"

    # Paths are rendered in POSIX form: a plan may be produced on one platform
    # and applied on another, and the receiving node only understands `/`.
    files = [
        plan_mod.FileSpec(paths.posix(directory / "ca.crt"), material.ca_cert, 0o644),
        plan_mod.FileSpec(paths.posix(directory / "ca.key"), material.ca_key, 0o600),
        plan_mod.FileSpec(paths.posix(directory / "server.crt"), material.server_cert, 0o644),
        plan_mod.FileSpec(paths.posix(directory / "server.key"), material.server_key, 0o600),
        plan_mod.FileSpec(paths.posix(directory / "ta.key"), material.tls_crypt, 0o600),
    ]
    if material.crl:
        files.append(
            plan_mod.FileSpec(paths.posix(directory / "crl.pem"), material.crl, 0o644)
        )

    for name, content in ccd.items():
        safe = paths.slugify(name, max_length=64)
        if not safe:
            continue
        files.append(
            plan_mod.FileSpec(paths.posix(directory / "ccd" / safe), content, 0o600)
        )

    # A placeholder keeps the directory present even with no static clients.
    files.append(plan_mod.FileSpec(paths.posix(directory / "ccd" / ".keep"), "", 0o600))

    files.append(
        plan_mod.FileSpec(
            paths.posix(directory / "server.conf"),
            openvpn.render_server_config(inbound),
            0o600,
        )
    )

    unit_name = openvpn.unit_name(inbound.tag)
    units = [
        plan_mod.UnitSpec(
            unit_name,
            openvpn.render_systemd_unit(inbound, binary=binary),
            reload=True,
        )
    ]

    commands: list[plan_mod.CommandSpec] = []
    if inbound.enabled:
        commands.append(plan_mod.CommandSpec(["systemctl", "enable", unit_name]))
        commands.append(
            plan_mod.CommandSpec(["systemctl", "restart" if _is_running(unit_name) else "start", unit_name])
        )
    else:
        commands.append(plan_mod.CommandSpec(["systemctl", "disable", "--now", unit_name], ignore_failure=True))

    return plan_mod.SidePlan(
        role="inbound",
        node_id=inbound.node_id,
        node_name=inbound.tag,
        files=files,
        units=units,
        commands=commands,
        service=unit_name,
        summary={
            "tag": inbound.tag,
            "listen_port": inbound.listen_port,
            "transport": inbound.transport,
            "directory": str(directory),
        },
    )


def _is_running(unit: str) -> bool:
    ok, _ = plan_mod.service_state(unit)
    return ok


def remove_inbound(payload: Mapping[str, Any]) -> dict[str, Any]:
    inbound = inbound_from_payload(payload["inbound"])
    unit = openvpn.unit_name(inbound.tag)
    report = {"service": unit, "ok": True, "removed": []}
    if shell.available("systemctl"):
        shell.run(["systemctl", "disable", "--now", unit], timeout=60)
    for target in (
        f"/etc/systemd/system/{unit}",
        str(openvpn.status_path(inbound.tag)),
        str(openvpn.management_socket(inbound.tag)),
    ):
        try:
            Path(target).unlink(missing_ok=True)
            report["removed"].append(target)
        except OSError as exc:
            report["ok"] = False
            report["removed"].append({"path": target, "error": str(exc)})
    if shell.available("systemctl"):
        shell.run(["systemctl", "daemon-reload"], timeout=30)
    directory = openvpn.inbound_dir(inbound)
    if directory.exists():
        shell.run(["rm", "-rf", str(directory)])
        report["removed"].append(str(directory))
    return report


def openvpn_sessions(tag: str) -> list[dict[str, Any]]:
    sessions = openvpn.read_status(tag)
    return [
        {
            "common_name": s.common_name,
            "real_address": s.real_address,
            "virtual_address": s.virtual_address,
            "bytes_received": s.bytes_received,
            "bytes_sent": s.bytes_sent,
            "connected_since": s.connected_since.isoformat() if s.connected_since else None,
            "username": s.username,
            "client_id": s.client_id,
        }
        for s in sessions
    ]


def openvpn_disconnect(tag: str, common_name: str) -> dict[str, Any]:
    return {"ok": openvpn.kill_session(tag, common_name)}


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------


def plan_tunnel_side(payload: Mapping[str, Any]) -> plan_mod.SidePlan:
    """Build the plan for one side of a tunnel on the target node."""
    from ..models import Node, Tunnel

    tunnel = Tunnel(
        id=int(payload.get("id", 0)),
        name=str(payload["name"]),
        engine=str(payload.get("engine", "native")),
        transport=str(payload["transport"]),
        mode=str(payload.get("mode", "reverse")),
        local_node_id=int(payload.get("local_node_id", 0)),
        remote_node_id=int(payload.get("remote_node_id", 0)),
        listen_port=int(payload.get("listen_port", 0)),
        interface_name=payload.get("interface_name"),
        mtu=int(payload.get("mtu", 1420)),
        options=dict(payload.get("options") or {}),
        forwarded_ports=list(payload.get("forwarded_ports") or []),
        token_enc=str(payload.get("token_enc", "")),
        enabled=bool(payload.get("enabled", True)),
    )
    local = Node(
        id=int(payload["local"]["id"]),
        name=str(payload["local"]["name"]),
        address=str(payload["local"].get("address") or ""),
    )
    remote = Node(
        id=int(payload["remote"]["id"]),
        name=str(payload["remote"]["name"]),
        address=str(payload["remote"].get("address") or ""),
    )
    side = str(payload.get("side", "local"))
    full = tunnels.plan_tunnel(tunnel, local, remote)
    return full.local if side == "local" else full.remote


def apply_plan(side: plan_mod.SidePlan) -> dict[str, Any]:
    return plan_mod.apply(side, validate_first=True)


def teardown_tunnel(payload: Mapping[str, Any]) -> dict[str, Any]:
    from ..models import Tunnel

    tunnel = Tunnel(
        id=int(payload.get("id", 0)),
        name=str(payload["name"]),
        engine=str(payload.get("engine", "native")),
        transport=str(payload["transport"]),
        mode=str(payload.get("mode", "reverse")),
        listen_port=int(payload.get("listen_port", 0)),
        interface_name=payload.get("interface_name"),
    )
    return tunnels.teardown(tunnel)


# ---------------------------------------------------------------------------
# Nginx front end
# ---------------------------------------------------------------------------


def apply_nginx(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Install the decoy site and, when asked, a shared-port SNI router."""
    result: dict[str, Any] = {"ok": True, "steps": []}

    title = str(payload.get("fake_site_title") or "Welcome")
    nginx.write_decoy_index(title)
    result["steps"].append({"step": "decoy_index", "ok": True})

    domain = str(payload.get("domain") or "")
    listen_ports = [int(p) for p in payload.get("http_ports") or [80]]
    site = nginx.render_decoy_site(title=title, domain=domain or "_", listen_ports=listen_ports)
    site_name = str(payload.get("name") or "decoy")
    target = paths.nginx_sites_dir() / f"panah-{paths.slugify(site_name)}.conf"
    paths.ensure(paths.nginx_sites_dir(), 0o755)
    paths.ensure(paths.nginx_enabled_dir(), 0o755)
    shell.write_file_locked(target, site, mode=0o644)
    link = paths.nginx_enabled_dir() / f"panah-{paths.slugify(site_name)}.conf"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    except OSError:
        shell.write_file_locked(link, site, mode=0o644)
    result["steps"].append({"step": "decoy_site", "path": str(target), "ok": True})

    router = payload.get("stream_router")
    if router:
        content = nginx.render_stream_router(
            listen_port=int(router["listen_port"]),
            routes=[
                nginx.Route(
                    server_name=str(route["server_name"]),
                    upstream=nginx.Upstream(
                        name=str(route["name"]),
                        host=str(route.get("host", "127.0.0.1")),
                        port=int(route["port"]),
                    ),
                )
                for route in router.get("routes", [])
            ],
            fallback_host=str(router.get("fallback_host", "127.0.0.1")),
            fallback_port=int(router.get("fallback_port", listen_ports[0] if listen_ports else 80)),
        )
        installed = nginx.install_stream_router(str(router.get("name", site_name)), content)
        result["steps"].append({"step": "stream_router", "path": str(installed), "ok": True})

    ok, output = nginx.reload_nginx()
    result["steps"].append({"step": "reload", "ok": ok, "message": output})
    result["ok"] = result["ok"] and ok
    return result


def remove_nginx(payload: Mapping[str, Any]) -> dict[str, Any]:
    name = paths.slugify(str(payload.get("name") or "decoy"))
    removed: list[str] = []
    for target in (
        paths.nginx_enabled_dir() / f"panah-{name}.conf",
        paths.nginx_sites_dir() / f"panah-{name}.conf",
        Path("/etc/nginx/stream.d") / f"panah-{name}.conf",
    ):
        try:
            target.unlink(missing_ok=True)
            removed.append(str(target))
        except OSError:
            pass
    ok, output = nginx.reload_nginx()
    return {"ok": ok, "message": output, "removed": removed}


def nginx_status() -> dict[str, Any]:
    return nginx.status()


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def service_status(unit: str) -> dict[str, Any]:
    """Report the state of a systemd unit without changing it."""
    try:
        plan_mod.validate_unit_name(unit)
    except plan_mod.PlanError as exc:
        raise ValueError(str(exc)) from exc
    active, state = plan_mod.service_state(unit)
    return {"unit": unit, "active": active, "state": state}
