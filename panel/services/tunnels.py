"""Tunnel engine.

Two engines are available:

``native``
    WireGuard and the kernel layer-3 tunnels (GRE, IPIP, SIT/6to4). No third
    party binary is required — WireGuard is configured through ``wg-quick`` and
    the rest through ``ip tunnel`` inside a dedicated systemd unit.

``backpack``
    The BackPack engine (https://github.com/AminMGMT/BackPack). Panah renders
    its documented TOML schema, installs ``/etc/backpack/<name>.toml`` plus a
    ``backpack-<name>.service`` unit, and drives it through the official
    non-interactive CLI.

Planning is pure: :func:`plan_tunnel` returns the artifacts for *both* ends so
the local host can apply its half directly and the same payload can be sent to
a remote node agent unchanged.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from ..core import crypto
from ..core.logging import get_logger
from ..models import Node, Tunnel
from . import paths, shell
from .plan import (
    CommandSpec,
    FileSpec,
    SidePlan,
    service_state,
)

log = get_logger("tunnels")

IFNAME_MAX = 15
STATE_DIR_NAME = "tunnels"

BACKPACK_BINARY = "/usr/local/bin/backpack"
BACKPACK_DIR = "/etc/backpack"


# ---------------------------------------------------------------------------
# Transport registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransportSpec:
    key: str
    label: str
    engine: str
    layer: str  # "l3" or "l4"
    encrypted: bool
    requirements: tuple[str, ...] = ()
    backpack_mode: str | None = None  # "reverse" | "direct"
    notes: str = ""


TRANSPORTS: dict[str, TransportSpec] = {
    # --- BackPack reverse transports (port forwarding, Iran -> Kharej) ---
    "stealth": TransportSpec("stealth", "TCP + Stealth", "backpack", "l4", True, backpack_mode="reverse"),
    "tcp": TransportSpec("tcp", "TCP", "backpack", "l4", False, backpack_mode="reverse"),
    "tcpmux": TransportSpec("tcpmux", "TCP Mux", "backpack", "l4", False, backpack_mode="reverse"),
    "pck": TransportSpec("pck", "TCP + PCK", "backpack", "l4", False, ("CAP_NET_RAW",), backpack_mode="reverse"),
    "udp": TransportSpec("udp", "UDP", "backpack", "l4", False, backpack_mode="reverse"),
    "kcp": TransportSpec("kcp", "UDP + KCP + FEC", "backpack", "l4", False, backpack_mode="reverse"),
    "quic": TransportSpec("quic", "UDP + QUIC", "backpack", "l4", False, backpack_mode="reverse"),
    "ws": TransportSpec("ws", "WebSocket", "backpack", "l4", False, backpack_mode="reverse"),
    "wsmux": TransportSpec("wsmux", "WebSocket Mux", "backpack", "l4", False, backpack_mode="reverse"),
    "wss": TransportSpec("wss", "WebSocket + TLS", "backpack", "l4", True, backpack_mode="reverse"),
    "wssmux": TransportSpec("wssmux", "WebSocket + TLS Mux", "backpack", "l4", True, backpack_mode="reverse"),
    "xdi": TransportSpec("xdi", "ICMP (xDi)", "backpack", "l4", False, ("CAP_NET_RAW",), backpack_mode="reverse"),
    # --- BackPack direct layer-3 carriers ---
    "bp-l3-udp": TransportSpec("bp-l3-udp", "BackPack L3 over UDP", "backpack", "l3", True, backpack_mode="direct"),
    "bp-l3-quic": TransportSpec("bp-l3-quic", "BackPack L3 over QUIC", "backpack", "l3", True, backpack_mode="direct"),
    "bp-l3-sni": TransportSpec("bp-l3-sni", "BackPack L3 + SNI spoof", "backpack", "l3", True, ("CAP_NET_RAW",), backpack_mode="direct"),
    "bp-l3-spoof": TransportSpec("bp-l3-spoof", "BackPack L3 + IP spoof", "backpack", "l3", True, ("CAP_NET_RAW",), backpack_mode="direct"),
    # --- Native engines ---
    "wireguard": TransportSpec(
        "wireguard", "WireGuard", "native", "l3", True, ("wireguard",),
        notes="Modern, fast and encrypted; recommended for new links.",
    ),
    "gre": TransportSpec(
        "gre", "GRE (kernel)", "native", "l3", False, ("gre",),
        notes="Unencrypted; use it to carry an encrypted protocol or on a trusted path.",
    ),
    "ipip": TransportSpec(
        "ipip", "IPIP (kernel)", "native", "l3", False, ("ipip",),
        notes="Unencrypted, lowest overhead.",
    ),
    "sit": TransportSpec(
        "sit", "SIT / 6to4 (kernel)", "native", "l3", False, ("sit",),
        notes="Carries IPv6 over IPv4; useful for legacy 6to4 style links.",
    ),
}


def transport_catalogue() -> list[dict[str, Any]]:
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "engine": spec.engine,
            "layer": spec.layer,
            "encrypted": spec.encrypted,
            "requirements": list(spec.requirements),
            "notes": spec.notes,
            "backpack_mode": spec.backpack_mode,
        }
        for spec in TRANSPORTS.values()
    ]


def get_transport(key: str) -> TransportSpec:
    spec = TRANSPORTS.get(key)
    if spec is None:
        raise TunnelError(f"unknown transport {key!r}")
    return spec


class TunnelError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TunnelPlan:
    engine: str
    transport: str
    mode: str
    local: SidePlan
    remote: SidePlan
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "transport": self.transport,
            "mode": self.mode,
            "local": self.local.to_dict(),
            "remote": self.remote.to_dict(),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def interface_name(tunnel: Tunnel) -> str:
    candidate = tunnel.interface_name.strip() if tunnel.interface_name else f"panah{tunnel.id or 0}"
    candidate = re.sub(r"[^A-Za-z0-9_.-]", "", candidate)
    return candidate[:IFNAME_MAX] or "panah0"


def unit_name(tunnel: Tunnel) -> str:
    return f"panah-tunnel-{paths.slugify(tunnel.name)}.service"


def backpack_unit_name(name: str) -> str:
    return f"backpack-{paths.slugify(name)}.service"


def shared_token(tunnel: Tunnel) -> str:
    if tunnel.token_enc:
        return crypto.decrypt(tunnel.token_enc)
    return ""


def ensure_token(tunnel: Tunnel) -> str:
    """Return the shared secret, generating and storing it on first use."""
    token = shared_token(tunnel)
    if token:
        return token
    import secrets

    token = secrets.token_urlsafe(32)
    tunnel.token_enc = crypto.encrypt(token)
    return token


def _address_pair(tunnel: Tunnel) -> tuple[str, str]:
    """Deterministic inner /31 style address pair for an L3 tunnel."""
    options = tunnel.options or {}
    local_ip = str(options.get("local_ip") or "")
    peer_ip = str(options.get("peer_ip") or "")
    if local_ip and peer_ip:
        return local_ip, peer_ip
    block = 66 + (int(tunnel.id or 0) % 128)
    octet = 1 + (int(tunnel.id or 0) // 128) % 250
    return f"10.{block}.{octet}.1", f"10.{block}.{octet}.2"


def _forwarded_ports(tunnel: Tunnel) -> list[str]:
    """BackPack style ``expose=target`` strings."""
    entries: list[str] = []
    for item in tunnel.forwarded_ports or []:
        if isinstance(item, Mapping):
            expose = item.get("expose") or item.get("listen")
            target = item.get("target") or item.get("to")
            protocol = item.get("protocol")
            if expose is None or target is None:
                continue
            token = f"{expose}={target}"
            entries.append(token if not protocol or protocol == "tcp" else f"{protocol}:{token}")
        elif isinstance(item, str) and "=" in item:
            entries.append(item)
    return entries


# ---------------------------------------------------------------------------
# WireGuard keys
# ---------------------------------------------------------------------------


def _new_wireguard_keypair() -> tuple[str, str]:
    private = x25519.X25519PrivateKey.generate()
    private_b64 = base64.b64encode(
        private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode("ascii")
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    return private_b64, public_b64


def wireguard_keypairs(tunnel: Tunnel, *, rotate: bool = False) -> dict[str, str]:
    """Fetch or create the key pairs for both ends (private keys encrypted)."""
    options = dict(tunnel.options or {})
    needed = ("wg_private_local", "wg_public_local", "wg_private_remote", "wg_public_remote")
    if rotate or any(not options.get(key) for key in needed):
        local_priv, local_pub = _new_wireguard_keypair()
        remote_priv, remote_pub = _new_wireguard_keypair()
        options.update(
            {
                "wg_private_local": crypto.encrypt(local_priv),
                "wg_public_local": local_pub,
                "wg_private_remote": crypto.encrypt(remote_priv),
                "wg_public_remote": remote_pub,
            }
        )
        tunnel.options = options

    return {
        "private_local": crypto.decrypt(options["wg_private_local"]),
        "public_local": options["wg_public_local"],
        "private_remote": crypto.decrypt(options["wg_private_remote"]),
        "public_remote": options["wg_public_remote"],
    }


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_tunnel(tunnel: Tunnel, local_node: Node, remote_node: Node) -> TunnelPlan:
    spec = get_transport(tunnel.transport)
    ensure_token(tunnel)
    if spec.engine == "native":
        if spec.key == "wireguard":
            plan = _plan_wireguard(tunnel, local_node, remote_node)
        else:
            plan = _plan_kernel(tunnel, local_node, remote_node, spec)
    else:
        plan = _plan_backpack(tunnel, local_node, remote_node, spec)
    plan.notes.extend(_endpoint_notes(tunnel, local_node, remote_node))
    return plan


def _endpoint_notes(tunnel: Tunnel, local_node: Node, remote_node: Node) -> list[str]:
    notes: list[str] = []
    if local_node.id == remote_node.id:
        notes.append("both ends are the same node; this link is a no-op")
    if not remote_node.address:
        notes.append("remote node has no address configured")
    if get_transport(tunnel.transport).engine == "backpack" and not shell.available("backpack"):
        notes.append(
            "BackPack binary not found on this host; install it on both nodes "
            "before starting the tunnel"
        )
    return notes


def _plan_wireguard(tunnel: Tunnel, local: Node, remote: Node) -> TunnelPlan:
    keys = wireguard_keypairs(tunnel)
    local_ip, peer_ip = _address_pair(tunnel)
    iface = interface_name(tunnel)
    mtu = tunnel.mtu or 1420
    listen_port = tunnel.listen_port

    local_conf = _wireguard_conf(
        private_key=keys["private_local"],
        address=f"{local_ip}/32",
        listen_port=listen_port,
        peer_public=keys["public_remote"],
        peer_allowed=f"{peer_ip}/32",
        peer_endpoint=None,  # local side listens
        mtu=mtu,
    )
    remote_conf = _wireguard_conf(
        private_key=keys["private_remote"],
        address=f"{peer_ip}/32",
        listen_port=0,
        peer_public=keys["public_local"],
        peer_allowed=f"{local_ip}/32",
        peer_endpoint=f"{_endpoint_host(local, tunnel)}:{listen_port}",
        mtu=mtu,
    )

    conf_dir = paths.wireguard_dir()
    local_side = SidePlan(
        role="local",
        node_id=local.id,
        node_name=local.name,
        files=[
            FileSpec(paths.posix(conf_dir / f"{iface}.conf"), local_conf, 0o600),
            FileSpec(
                paths.posix(
                    paths.data_dir() / STATE_DIR_NAME / f"{paths.slugify(tunnel.name)}.wg.json"
                ),
                json.dumps({"interface": iface, "address": local_ip, "peer": peer_ip}, indent=2),
                0o600,
            ),
        ],
        commands=[CommandSpec(["systemctl", "enable", "--now", f"wg-quick@{iface}.service"])],
        service=f"wg-quick@{iface}.service",
        summary={"interface": iface, "address": local_ip, "peer": peer_ip, "listen_port": listen_port},
    )
    remote_side = SidePlan(
        role="remote",
        node_id=remote.id,
        node_name=remote.name,
        files=[FileSpec(paths.posix(conf_dir / f"{iface}.conf"), remote_conf, 0o600)],
        commands=[CommandSpec(["systemctl", "enable", "--now", f"wg-quick@{iface}.service"])],
        service=f"wg-quick@{iface}.service",
        summary={"interface": iface, "address": peer_ip, "peer": local_ip},
    )
    return TunnelPlan("native", tunnel.transport, tunnel.mode, local_side, remote_side)


def _wireguard_conf(
    *,
    private_key: str,
    address: str,
    listen_port: int,
    peer_public: str,
    peer_allowed: str,
    peer_endpoint: str | None,
    mtu: int,
) -> str:
    lines = [
        "# Managed by Panah - do not edit by hand.",
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {address}",
        f"MTU = {mtu}",
    ]
    if listen_port:
        lines.append(f"ListenPort = {listen_port}")
    lines.extend(
        [
            "PostUp = sysctl -w net.ipv4.ip_forward=1",
            "PostUp = iptables -A FORWARD -i %i -j ACCEPT",
            "PostUp = iptables -A FORWARD -o %i -j ACCEPT",
            "PostDown = iptables -D FORWARD -i %i -j ACCEPT",
            "PostDown = iptables -D FORWARD -o %i -j ACCEPT",
            "",
            "[Peer]",
            f"PublicKey = {peer_public}",
            f"AllowedIPs = {peer_allowed}",
            "PersistentKeepalive = 25",
        ]
    )
    if peer_endpoint:
        lines.append(f"Endpoint = {peer_endpoint}")
    return "\n".join(lines) + "\n"


def _endpoint_host(node: Node, tunnel: Tunnel) -> str:
    """Address the dialing side should reach on the listening node."""
    options = tunnel.options or {}
    override = options.get("remote_address")
    if override:
        return str(override)
    return node.address


def _plan_kernel(
    tunnel: Tunnel, local: Node, remote: Node, spec: TransportSpec
) -> TunnelPlan:
    local_ip, peer_ip = _address_pair(tunnel)
    iface = interface_name(tunnel)
    mtu = tunnel.mtu or 1400
    listen_port = tunnel.listen_port
    mode = spec.key  # gre | ipip | sit

    def commands_for(local_public: str, remote_public: str) -> list[CommandSpec]:
        return [
            CommandSpec(["modprobe", mode], ignore_failure=True),
            CommandSpec(
                [
                    "ip",
                    "tunnel",
                    "add",
                    iface,
                    "mode",
                    mode,
                    "local",
                    local_public,
                    "remote",
                    remote_public,
                    "ttl",
                    "255",
                ]
            ),
            CommandSpec(["ip", "link", "set", iface, "mtu", str(mtu), "up"]),
            CommandSpec(["ip", "addr", "add", f"{local_ip}/32", "peer", f"{peer_ip}/32", "dev", iface]),
            CommandSpec(["ip", "route", "replace", f"{peer_ip}/32", "dev", iface]),
        ]

    local_cmds = commands_for(local.address, _endpoint_host(remote, tunnel))
    remote_cmds = commands_for(remote.address or "", _endpoint_host(local, tunnel))

    unit = _kernel_unit(
        name=f"Panah tunnel {tunnel.name}",
        iface=iface,
        up=[c.args for c in local_cmds],
        down=[["ip", "link", "del", iface]],
    )
    remote_unit = _kernel_unit(
        name=f"Panah tunnel {tunnel.name}",
        iface=iface,
        up=[c.args for c in remote_cmds],
        down=[["ip", "link", "del", iface]],
    )

    name = unit_name(tunnel)
    local_side = SidePlan(
        role="local",
        node_id=local.id,
        node_name=local.name,
        files=[FileSpec(f"/etc/systemd/system/{name}", unit, 0o644)],
        commands=[
            CommandSpec(["systemctl", "daemon-reload"]),
            CommandSpec(["systemctl", "enable", "--now", name]),
        ],
        service=name,
        summary={"interface": iface, "address": local_ip, "peer": peer_ip, "mode": mode, "port": listen_port},
    )
    remote_side = SidePlan(
        role="remote",
        node_id=remote.id,
        node_name=remote.name,
        files=[FileSpec(f"/etc/systemd/system/{name}", remote_unit, 0o644)],
        commands=[
            CommandSpec(["systemctl", "daemon-reload"]),
            CommandSpec(["systemctl", "enable", "--now", name]),
        ],
        service=name,
        summary={"interface": iface, "address": peer_ip, "peer": local_ip, "mode": mode},
    )
    return TunnelPlan(
        "native",
        tunnel.transport,
        tunnel.mode,
        local_side,
        remote_side,
        notes=["kernel tunnels are unencrypted; keep sensitive traffic inside an encrypted layer"],
    )


# Absolute paths so the generated unit never depends on PATH. Ubuntu 24.04
# merges /sbin into /usr/sbin, so the /sbin forms resolve on every supported
# release while keeping the unit portable between hosts.
_BIN_PATHS = {
    "modprobe": "/sbin/modprobe",
    "ip": "/sbin/ip",
    "iptables": "/sbin/iptables",
    "sysctl": "/sbin/sysctl",
    "wg": "/usr/bin/wg",
}


def _abs(binary: str) -> str:
    return _BIN_PATHS.get(binary, f"/usr/bin/{binary}")


def _kernel_unit(*, name: str, iface: str, up: Iterable[list[str]], down: Iterable[list[str]]) -> str:
    """Render a oneshot unit from explicit argument vectors.

    Each vector becomes its own ``ExecStart`` line so no shell quoting is
    involved; a leading ``-`` lets optional steps (module loading) fail
    harmlessly.
    """
    lines = [
        "[Unit]",
        f"Description={name}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "RemainAfterExit=yes",
    ]
    for argv in up:
        if not argv:
            continue
        rendered = " ".join([_abs(str(argv[0])), *[str(a) for a in argv[1:]]])
        prefix = "-" if argv[0] == "modprobe" else ""
        lines.append(f"ExecStart={prefix}{rendered}")
    stop_lines = []
    for argv in down:
        if not argv:
            continue
        rendered = " ".join([_abs(str(argv[0])), *[str(a) for a in argv[1:]]])
        # Leading "-" makes teardown tolerant of an already-removed interface.
        stop_lines.append(f"ExecStop=-{rendered}")
    lines.extend(stop_lines or [f"ExecStop=-/sbin/ip link del {iface}"])
    lines.extend(["", "[Install]", "WantedBy=multi-user.target"])
    return "\n".join(lines) + "\n"


def _plan_backpack(
    tunnel: Tunnel, local: Node, remote: Node, spec: TransportSpec
) -> TunnelPlan:
    token = shared_token(tunnel)
    ports = _forwarded_ports(tunnel)
    slug = paths.slugify(tunnel.name)
    # Always derived from the configured root: a plan must sit inside the
    # directories the node is allowed to write to.
    conf_path = f"{paths.posix(paths.backpack_dir())}/{slug}.toml"
    unit = backpack_unit_name(tunnel.name)

    if spec.backpack_mode == "direct" or tunnel.mode == "direct":
        local_conf = _backpack_direct_toml(tunnel, spec, token, role="dial", peer=remote)
        remote_conf = _backpack_direct_toml(tunnel, spec, token, role="listen", peer=local)
    else:
        local_conf = _backpack_server_toml(tunnel, spec, token, ports)
        remote_conf = _backpack_client_toml(tunnel, spec, token, local)

    unit_content = backpack_service_unit(tunnel.name, conf_path, slug)

    local_side = SidePlan(
        role="local",
        node_id=local.id,
        node_name=local.name,
        files=[
            FileSpec(conf_path, local_conf, 0o600),
            FileSpec(f"/etc/systemd/system/{unit}", unit_content, 0o644),
        ],
        commands=[
            CommandSpec(["systemctl", "daemon-reload"]),
            CommandSpec(["systemctl", "enable", "--now", unit]),
        ],
        service=unit,
        summary={
            "config": conf_path,
            "transport": spec.key,
            "forwarded_ports": ports,
            "listen_port": tunnel.listen_port,
        },
    )
    remote_side = SidePlan(
        role="remote",
        node_id=remote.id,
        node_name=remote.name,
        files=[
            FileSpec(conf_path, remote_conf, 0o600),
            FileSpec(f"/etc/systemd/system/{unit}", unit_content, 0o644),
        ],
        commands=[
            CommandSpec(["systemctl", "daemon-reload"]),
            CommandSpec(["systemctl", "enable", "--now", unit]),
        ],
        service=unit,
        summary={"config": conf_path, "transport": spec.key},
    )
    return TunnelPlan("backpack", tunnel.transport, tunnel.mode, local_side, remote_side)


def backpack_service_unit(name: str, conf_path: str, slug: str) -> str:
    return (
        "[Unit]\n"
        f"Description=BackPack tunnel {name}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=" + BACKPACK_BINARY + f" -c {conf_path}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "LimitNOFILE=1048576\n"
        "AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE\n"
        "CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE\n"
        "NoNewPrivileges=false\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


# ASCII only: these files are parsed by tools that are not guaranteed to be
# UTF-8 aware.
_TOML_HEADER = "# Managed by Panah - generated from the tunnel editor.\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_table(section: str, values: Mapping[str, Any]) -> str:
    lines = [f"[{section}]"]
    for key, value in values.items():
        if value is None or value == "" or value == []:
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines)


def _backpack_transport_key(spec: TransportSpec, mode: str) -> str:
    if spec.backpack_mode == "direct":
        # bp-l3-udp -> udp, bp-l3-quic -> quic, bp-l3-sni -> sni, bp-l3-spoof -> spoof
        return spec.key.replace("bp-l3-", "")
    if mode == "direct":
        return spec.key
    return spec.key


def _backpack_server_toml(
    tunnel: Tunnel, spec: TransportSpec, token: str, ports: list[str]
) -> str:
    options = tunnel.options or {}
    values: dict[str, Any] = {
        "bind_addr": f"{options.get('bind_addr', '0.0.0.0')}:{tunnel.listen_port}",
        "transport": _backpack_transport_key(spec, "reverse"),
        "token": token,
        "ports": ports,
        "accept_udp": bool(options.get("accept_udp", False)),
        "log_level": str(options.get("log_level", "info")),
        "preset": options.get("preset"),
        "so_rcvbuf": options.get("so_rcvbuf"),
        "so_sndbuf": options.get("so_sndbuf"),
        "nodelay": bool(options.get("nodelay", True)),
        "heartbeat": options.get("heartbeat"),
        "channel_size": options.get("channel_size"),
        "max_connections": options.get("max_connections"),
        "simple_auth": bool(options.get("simple_auth", False)),
        "fallback_transports": options.get("fallback_transports") or [],
        "fallback_dwell": options.get("fallback_dwell"),
        "tls_cert": options.get("tls_cert"),
        "tls_key": options.get("tls_key"),
        "acme_domain": options.get("acme_domain"),
        "acme_email": options.get("acme_email"),
        "sniffer": bool(options.get("sniffer", False)),
        "proxy_protocol": bool(options.get("proxy_protocol", False)),
    }
    if spec.key == "kcp":
        values.update({k: options.get(k) for k in (
            "kcp_mtu", "kcp_interval", "kcp_resend", "kcp_nocongestion", "kcp_nodelay",
            "kcp_acknodelay", "kcp_sndwnd", "kcp_rcvwnd", "kcp_datashards", "kcp_parityshards",
        )})
    if spec.key == "pck":
        values.update({k: options.get(k) for k in ("pck_interface", "pck_gateway_mac", "pck_flags")})
    return _TOML_HEADER + _toml_table("server", values) + "\n"


def _backpack_client_toml(
    tunnel: Tunnel, spec: TransportSpec, token: str, local: Node
) -> str:
    options = tunnel.options or {}
    values: dict[str, Any] = {
        "remote_addr": f"{_endpoint_host(local, tunnel)}:{tunnel.listen_port}",
        "transport": _backpack_transport_key(spec, "reverse"),
        "token": token,
        "retry_interval": options.get("retry_interval", 3),
        "connection_pool": options.get("connection_pool", 8),
        "log_level": str(options.get("log_level", "info")),
        "preset": options.get("preset"),
        "so_rcvbuf": options.get("so_rcvbuf"),
        "so_sndbuf": options.get("so_sndbuf"),
        "nodelay": bool(options.get("nodelay", True)),
        "keepalive_period": options.get("keepalive_period"),
        "mss": options.get("mss"),
        "simple_auth": bool(options.get("simple_auth", False)),
        "fallback_addrs": options.get("fallback_addrs") or [],
        "fallback_transports": options.get("fallback_transports") or [],
        "fallback_dwell": options.get("fallback_dwell"),
        "health_failover": bool(options.get("health_failover", False)),
        "interface": options.get("interface"),
    }
    if spec.key == "kcp":
        values.update({k: options.get(k) for k in (
            "kcp_mtu", "kcp_interval", "kcp_resend", "kcp_nocongestion", "kcp_nodelay",
            "kcp_acknodelay", "kcp_sndwnd", "kcp_rcvwnd", "kcp_datashards", "kcp_parityshards",
        )})
    if spec.key == "pck":
        values.update({k: options.get(k) for k in ("pck_interface", "pck_gateway_mac", "pck_flags")})
    return _TOML_HEADER + _toml_table("client", values) + "\n"


def _backpack_direct_toml(
    tunnel: Tunnel, spec: TransportSpec, token: str, *, role: str, peer: Node
) -> str:
    options = tunnel.options or {}
    local_ip, peer_ip = _address_pair(tunnel)
    if role == "dial":
        addr = f"{_endpoint_host(peer, tunnel)}:{tunnel.listen_port}"
        my_ip, their_ip = local_ip, peer_ip
    else:
        addr = f"{options.get('bind_addr', '0.0.0.0')}:{tunnel.listen_port}"
        my_ip, their_ip = peer_ip, local_ip

    values: dict[str, Any] = {
        "mode": role,
        "addr": addr,
        "carrier": _backpack_transport_key(spec, "direct"),
        "token": token,
        "iface": interface_name(tunnel),
        "mtu": tunnel.mtu or 1400,
        "local_ip": f"{my_ip}/24",
        "peer_ip": their_ip,
        "qdisc": "fq_codel",
        "paths": options.get("paths", 1),
        "sockbuf": options.get("sockbuf"),
        "fec_data": options.get("fec_data"),
        "fec_parity": options.get("fec_parity"),
        "gre_key": options.get("gre_key"),
        "sni_domain": options.get("sni_domain"),
        "ports": _forwarded_ports(tunnel),
        "log_level": str(options.get("log_level", "info")),
        "preset": options.get("preset"),
        "txqueuelen": options.get("txqueuelen"),
    }
    if spec.key == "bp-l3-spoof":
        values.update({k: options.get(k) for k in (
            "spoof_profile", "spoof_uplink", "spoof_downlink", "spoof_src_pool",
            "spoof_peer_ip", "spoof_src_ip", "spoof_interface", "spoof_padding",
        )})
    return _TOML_HEADER + _toml_table("l3", values) + "\n"


# ---------------------------------------------------------------------------
# Applying a plan on the local host
# ---------------------------------------------------------------------------


def teardown(tunnel: Tunnel) -> dict[str, Any]:
    """Stop and remove everything the plan created for this tunnel."""
    spec = get_transport(tunnel.transport)
    results: dict[str, Any] = {}
    name = unit_name(tunnel)

    if spec.engine == "backpack":
        unit = backpack_unit_name(tunnel.name)
        shell.run(["systemctl", "disable", "--now", unit], timeout=60)
        slug = paths.slugify(tunnel.name)
        for target in (
            f"{paths.posix(paths.backpack_dir())}/{slug}.toml",
            f"/etc/systemd/system/{unit}",
        ):
            try:
                paths.Path(target).unlink()
            except OSError:
                pass
        shell.run(["systemctl", "daemon-reload"], timeout=30)
        results["service"] = unit
        return results

    iface = interface_name(tunnel)
    if spec.key == "wireguard":
        shell.run(["systemctl", "disable", "--now", f"wg-quick@{iface}.service"], timeout=60)
        try:
            (paths.wireguard_dir() / f"{iface}.conf").unlink()
        except OSError:
            pass
    else:
        shell.run(["systemctl", "disable", "--now", name], timeout=60)
        shell.run(["ip", "link", "del", iface], timeout=30)
        try:
            paths.Path(f"/etc/systemd/system/{name}").unlink()
        except OSError:
            pass
        shell.run(["systemctl", "daemon-reload"], timeout=30)
    results["service"] = name
    results["interface"] = iface
    return results


def side_status(service: str | None) -> tuple[bool, str]:
    return service_state(service or "")


# ---------------------------------------------------------------------------
# BackPack CLI adapter
# ---------------------------------------------------------------------------


def backpack_available() -> bool:
    return shell.available("backpack")


def backpack_info() -> dict[str, Any]:
    if not backpack_available():
        return {"installed": False, "version": None, "tunnels": []}
    version = shell.run(["backpack", "version", "--json"], timeout=10)
    tunnels = shell.run(["backpack", "tunnel", "list", "--json"], timeout=15)
    payload: Any = None
    if tunnels.ok:
        try:
            payload = json.loads(tunnels.output)
        except json.JSONDecodeError:
            payload = None
    return {
        "installed": True,
        "version": version.output if version.ok else None,
        "tunnels": payload if isinstance(payload, list) else [],
    }


def backpack_check(config_path: str) -> tuple[bool, str]:
    if not backpack_available():
        return False, "backpack is not installed"
    result = shell.run(["backpack", "check", "-c", config_path], timeout=30)
    return result.ok, result.message


def backpack_tunnel_status(name: str) -> dict[str, Any]:
    if not backpack_available():
        return {"ok": False, "error": "backpack is not installed"}
    result = shell.run(["backpack", "tunnel", "status", name, "--json"], timeout=15)
    if not result.ok:
        return {"ok": False, "error": result.message}
    try:
        return {"ok": True, "status": json.loads(result.output)}
    except json.JSONDecodeError:
        return {"ok": True, "status": result.output}


def backpack_link_apply(link: str, *, name: str | None = None, host: str | None = None) -> tuple[bool, str]:
    """Hand a setup link from the other end to BackPack's own installer."""
    if not backpack_available():
        return False, "backpack is not installed"
    if not link.startswith("backpack://"):
        return False, "not a backpack setup link"
    args = ["backpack", "link", "apply"]
    if name:
        args += ["--name", name]
    if host:
        args += ["--host", host]
    args.append(link)
    result = shell.run(args, timeout=120)
    return result.ok, result.message


def backpack_node_exec(operation: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Drive a managed BackPack node over its documented base64 JSON protocol.

    ``backpack node exec -`` reads one base64 encoded ``{op, body}`` envelope
    from stdin and answers with a base64 encoded JSON response.
    """
    if not backpack_available():
        return {"ok": False, "error": "backpack is not installed"}
    allowed = {
        "hello", "apply", "list", "status", "settings", "logs",
        "linktest", "delete", "start", "stop", "restart",
    }
    if operation not in allowed:
        raise TunnelError(f"unsupported backpack operation {operation!r}")

    envelope = json.dumps({"op": operation, "body": dict(body or {})}).encode("utf-8")
    encoded = base64.b64encode(envelope).decode("ascii")
    result = shell.run(["backpack", "node", "exec", "-"], input_text=encoded + "\n", timeout=60)
    if not result.ok:
        return {"ok": False, "error": result.message, "stdout": result.stdout[-500:]}
    try:
        decoded = base64.b64decode(result.output.strip() or "")
        return {"ok": True, "response": json.loads(decoded)}
    except (ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"could not decode node response: {exc}"}


def link_status(link: str) -> dict[str, Any]:
    """Validate a setup link without applying it."""
    if not link.startswith("backpack://"):
        return {"ok": False, "error": "not a backpack setup link"}
    version = link[len("backpack://") :].split(".", 1)[0]
    if version not in {"1", "2"}:
        return {"ok": False, "error": f"unsupported setup link version {version!r}"}
    if not backpack_available():
        return {"ok": True, "version": version, "warning": "backpack is not installed locally"}
    return {"ok": True, "version": version}
