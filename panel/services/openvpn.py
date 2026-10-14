"""OpenVPN core.

One systemd unit per inbound keeps a failing tunnel from taking the whole host
down and lets the panel restart a single listener. All certificate material is
produced by :mod:`panel.services.pki`, so the server needs no easy-rsa.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from ..core.logging import get_logger
from ..models import Client, ClientCredential, Inbound, PkiMaterial
from . import paths, pki, shell

log = get_logger("openvpn")

UNIT_PREFIX = "openvpn-panah-"
SERVICE_TEMPLATE_PATH = Path("/lib/systemd/system")
DEFAULT_TUN_MTU = 1500

DEFAULT_SETTINGS: dict[str, Any] = {
    "mtu": 1500,
    "max_clients": 500,
    "keepalive_interval": 10,
    "keepalive_timeout": 60,
    "data_ciphers": "AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",
    "data_ciphers_fallback": "AES-256-CBC",
    "auth_digest": "SHA256",
    "tls_version_min": "1.2",
    "reneg_sec": 0,
    "verbosity": 3,
    "compress": "none",
    # Routes pushed to clients (default: only the tunnel network).
    "push_routes": [],
    "redirect_gateway": True,
    "dns_servers": ["1.1.1.1", "8.8.8.8"],
    # DPI evasion: let OpenVPN hand non-VPN TCP traffic to nginx.
    "port_share": False,
    "port_share_target": "127.0.0.1:8443",
    # Statically allocate a predictable address per client.
    "static_addresses": True,
    "duplicate_cn": False,
    "sndbuf": 393216,
    "rcvbuf": 393216,
    "txqueuelen": 4000,
}


class OpenVPNError(RuntimeError):
    """Raised when the host cannot satisfy an OpenVPN operation."""


@dataclass(slots=True)
class Session:
    common_name: str
    real_address: str = ""
    virtual_address: str = ""
    bytes_received: int = 0
    bytes_sent: int = 0
    connected_since: datetime | None = None
    username: str = ""
    client_id: str = ""


@dataclass(slots=True)
class Capabilities:
    installed: bool
    version: str | None = None
    binary: str | None = None
    has_dco: bool = False
    has_tun: bool = False
    systemd: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "version": self.version,
            "binary": self.binary,
            "dco": self.has_dco,
            "tun": self.has_tun,
            "systemd": self.systemd,
            "detail": self.detail,
        }


@dataclass(slots=True)
class Material:
    """All PEM material needed to run one inbound."""

    ca_cert: str
    ca_key: str
    server_cert: str
    server_key: str
    tls_crypt: str
    crl: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def as_map(self) -> dict[str, tuple[str, bool]]:
        """Return ``kind -> (content, is_secret)``."""
        return {
            "ca_cert": (self.ca_cert, False),
            "ca_key": (self.ca_key, True),
            "server_cert": (self.server_cert, False),
            "server_key": (self.server_key, True),
            "tls_crypt": (self.tls_crypt, True),
            "crl": (self.crl, False),
        }


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _list_setting(value: Any) -> list[str]:
    """Coerce a list-valued setting.

    The API accepts both a list and a comma separated string for these keys, and
    a stored value may predate that validation. Iterating a bare string here
    would emit one config line per character, so everything is normalised in one
    place before a renderer ever sees it.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


#: Settings that always render as a list of strings.
LIST_SETTINGS = ("push_routes", "dns_servers")


def effective_settings(inbound: Inbound) -> dict[str, Any]:
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in (inbound.settings or {}).items() if v is not None})
    for key in LIST_SETTINGS:
        if key in merged:
            merged[key] = _list_setting(merged[key])
    return merged


def validate_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce and bound inbound settings coming from the API."""
    cleaned: dict[str, Any] = {}
    int_ranges = {
        "mtu": (576, 1500),
        "max_clients": (1, 10000),
        "keepalive_interval": (5, 120),
        "keepalive_timeout": (10, 600),
        "reneg_sec": (0, 86400),
        "verbosity": (0, 6),
        "sndbuf": (0, 8 * 1024 * 1024),
        "rcvbuf": (0, 8 * 1024 * 1024),
        "txqueuelen": (0, 20000),
    }
    for key, (low, high) in int_ranges.items():
        if key not in payload or payload[key] is None:
            continue
        try:
            value = int(payload[key])
        except (TypeError, ValueError) as exc:
            raise OpenVPNError(f"setting {key} must be an integer") from exc
        if not low <= value <= high:
            raise OpenVPNError(f"setting {key} must be between {low} and {high}")
        cleaned[key] = value

    if payload.get("data_ciphers"):
        ciphers = [c.strip().upper() for c in str(payload["data_ciphers"]).split(":") if c.strip()]
        allowed = {
            "AES-256-GCM",
            "AES-128-GCM",
            "CHACHA20-POLY1305",
            "AES-256-CBC",
            "AES-192-CBC",
            "AES-128-CBC",
            "NONE",
        }
        unknown = [c for c in ciphers if c not in allowed]
        if unknown:
            raise OpenVPNError(f"unsupported data cipher(s): {', '.join(unknown)}")
        cleaned["data_ciphers"] = ":".join(ciphers)

    if payload.get("auth_digest"):
        digest = str(payload["auth_digest"]).upper()
        if digest not in {"SHA256", "SHA512", "SHA1"}:
            raise OpenVPNError("auth_digest must be SHA256, SHA512 or SHA1")
        cleaned["auth_digest"] = digest

    if "redirect_gateway" in payload:
        cleaned["redirect_gateway"] = bool(payload["redirect_gateway"])
    if "port_share" in payload:
        cleaned["port_share"] = bool(payload["port_share"])
    if "static_addresses" in payload:
        cleaned["static_addresses"] = bool(payload["static_addresses"])
    if "duplicate_cn" in payload:
        cleaned["duplicate_cn"] = bool(payload["duplicate_cn"])

    if payload.get("dns_servers") is not None:
        servers = payload["dns_servers"]
        if isinstance(servers, str):
            servers = [s.strip() for s in servers.split(",") if s.strip()]
        validated: list[str] = []
        for server in servers:
            candidate = str(server).strip()
            if not re.fullmatch(r"[0-9A-Fa-f:.]{3,45}", candidate):
                raise OpenVPNError(f"invalid DNS server {candidate!r}")
            validated.append(candidate)
        cleaned["dns_servers"] = validated[:4]

    if payload.get("push_routes") is not None:
        routes = payload["push_routes"]
        if isinstance(routes, str):
            routes = [r.strip() for r in routes.replace(";", ",").split(",") if r.strip()]
        validated_routes: list[str] = []
        for route in routes:
            candidate = str(route).strip()
            if not _valid_route(candidate):
                raise OpenVPNError(f"invalid route {candidate!r} (expected CIDR like 10.0.0.0/8)")
            validated_routes.append(candidate)
        cleaned["push_routes"] = validated_routes[:32]

    if payload.get("port_share_target"):
        target = str(payload["port_share_target"]).strip()
        host, _, port = target.rpartition(":")
        if not host or not port.isdigit():
            raise OpenVPNError("port_share_target must look like 127.0.0.1:8443")
        cleaned["port_share_target"] = f"{host}:{int(port)}"

    return cleaned


def _route_push(candidate: str) -> str | None:
    """Render one ``push "route ..."`` line from a CIDR.

    OpenVPN expects a network address plus a *dotted* netmask; the shorthand
    ``10.9.0.0/24`` is not a valid route argument, and anything unparseable is
    dropped rather than written into a config the daemon will refuse.
    """
    try:
        network = ipaddress.ip_network(candidate.strip(), strict=False)
    except ValueError:
        log.warning("ignoring unparseable push route %r", candidate)
        return None
    if network.version == 6:
        return f'push "route-ipv6 {network.with_prefixlen}"'
    return f'push "route {network.network_address} {network.netmask}"'


def _valid_route(candidate: str) -> bool:
    parts = candidate.split("/")
    if len(parts) != 2:
        return False
    address, prefix = parts
    if not prefix.isdigit() or not 0 <= int(prefix) <= 128:
        return False
    return bool(re.fullmatch(r"[0-9A-Fa-f:.]{3,45}", address))


# ---------------------------------------------------------------------------
# Paths and units
# ---------------------------------------------------------------------------


def inbound_dir(inbound: Inbound) -> Path:
    return paths.openvpn_root() / paths.slugify(inbound.tag)


def unit_name(tag: str) -> str:
    return f"{UNIT_PREFIX}{paths.slugify(tag)}.service"


def status_path(tag: str) -> Path:
    return paths.run_dir() / f"{paths.slugify(tag)}.status"


def management_socket(tag: str) -> Path:
    return paths.run_dir() / f"{paths.slugify(tag)}.mgmt"


def _run_dir() -> Path:
    return paths.ensure(paths.run_dir(), 0o755)


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


def detect() -> Capabilities:
    binary = shell.which("openvpn")
    if not binary:
        return Capabilities(installed=False, detail="openvpn is not installed")

    result = shell.run([binary, "--version"], timeout=10)
    version = None
    if result.ok:
        match = re.search(r"OpenVPN\s+([0-9.]+)", result.stdout)
        if match:
            version = match.group(1)

    has_tun = Path("/dev/net/tun").exists()
    systemd = shell.available("systemctl")
    has_dco = False
    if version:
        try:
            has_dco = int(version.split(".")[0]) >= 3 or version.startswith("2.6")
        except ValueError:  # pragma: no cover
            has_dco = False

    detail = []
    if not has_tun:
        detail.append("tun module missing")
    if not systemd:
        detail.append("systemd unavailable")

    return Capabilities(
        installed=True,
        version=version,
        binary=binary,
        has_dco=has_dco,
        has_tun=has_tun,
        systemd=systemd,
        detail="; ".join(detail),
    )


# ---------------------------------------------------------------------------
# PKI
# ---------------------------------------------------------------------------


def _material_from_db(session: OrmSession, inbound_id: int) -> dict[str, str]:
    rows = session.scalars(
        select(PkiMaterial).where(PkiMaterial.inbound_id == inbound_id)
    ).all()
    return {row.kind: row.content for row in rows}


def _store_material(session: OrmSession, inbound_id: int, material: Material) -> None:
    existing = {row.kind: row for row in session.scalars(
        select(PkiMaterial).where(PkiMaterial.inbound_id == inbound_id)
    )}
    for kind, (content, is_secret) in material.as_map().items():
        if not content:
            continue
        row = existing.get(kind)
        if row:
            row.content = content
            row.is_secret = is_secret
        else:
            session.add(
                PkiMaterial(
                    inbound_id=inbound_id,
                    kind=kind,
                    content=content,
                    is_secret=is_secret,
                )
            )
    if material.crl and "crl" in existing:
        existing["crl"].content = material.crl


def ensure_material(
    session: OrmSession, inbound: Inbound, *, rotate: bool = False
) -> Material:
    """Load the inbound PKI, generating it on first use."""
    stored = _material_from_db(session, inbound.id)
    if rotate or not stored.get("ca_cert") or not stored.get("server_cert"):
        ca = pki.create_ca(common_name=f"Panah CA {inbound.tag}")
        hostnames = [h for h in (inbound.domain, inbound.sni, "localhost") if h]
        server = pki.create_server_certificate(
            ca.certificate_pem, ca.private_key_pem, f"server-{inbound.tag}", hostnames
        )
        material = Material(
            ca_cert=ca.certificate_pem,
            ca_key=ca.private_key_pem,
            server_cert=server.certificate_pem,
            server_key=server.private_key_pem,
            tls_crypt=pki.generate_tls_crypt_key(),
            crl="",
        )
        _store_material(session, inbound.id, material)
        session.flush()
        material.crl = refresh_crl(session, inbound)
        _store_material(session, inbound.id, material)
        log.info("generated PKI for inbound %s", inbound.tag)
        return material

    return Material(
        ca_cert=stored["ca_cert"],
        ca_key=stored.get("ca_key", ""),
        server_cert=stored["server_cert"],
        server_key=stored.get("server_key", ""),
        tls_crypt=stored.get("tls_crypt", pki.generate_tls_crypt_key()),
        crl=stored.get("crl", ""),
    )


def refresh_crl(session: OrmSession, inbound: Inbound) -> str:
    """Rebuild the CRL from every revoked client certificate of this inbound."""
    material = _material_from_db(session, inbound.id)
    ca_cert = material.get("ca_cert")
    ca_key = material.get("ca_key")
    if not ca_cert or not ca_key:
        return ""

    revoked: list[tuple[str, datetime | None]] = []
    rows = session.execute(
        select(ClientCredential.serial, ClientCredential.updated_at)
        .join(Client, Client.id == ClientCredential.client_id)
        .where(Client.inbound_id == inbound.id, ClientCredential.revoked.is_(True))
    ).all()
    for serial, updated_at in rows:
        if serial:
            revoked.append((serial, updated_at))
    return pki.build_crl(ca_cert, ca_key, revoked)


def issue_client_certificate(
    session: OrmSession, inbound: Inbound, client: Client
) -> tuple[str, str, str, datetime | None]:
    """Create (or refresh) the certificate/key pair for ``client``."""
    material = ensure_material(session, inbound)
    pair = pki.create_client_certificate(
        material.ca_cert,
        material.ca_key,
        client.common_name,
        validity_days=_client_validity_days(client),
    )
    _upsert_credential(session, client.id, "cert", pair.certificate_pem, False, pair.serial, pair.not_after)
    _upsert_credential(session, client.id, "key", pair.private_key_pem, True, pair.serial, pair.not_after)
    material.crl = refresh_crl(session, inbound)
    _store_material(session, inbound.id, material)
    return pair.certificate_pem, pair.private_key_pem, pair.serial, pair.not_after


def _client_validity_days(client: Client) -> int:
    if client.expire_at:
        remaining = (client.expire_at - datetime.now(UTC)).days + 1
        return max(1, min(pki.CLIENT_VALIDITY_DAYS, remaining))
    return pki.CLIENT_VALIDITY_DAYS


def _upsert_credential(
    session: OrmSession,
    client_id: int,
    kind: str,
    content: str,
    is_secret: bool,
    serial: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    row = session.scalar(
        select(ClientCredential).where(
            ClientCredential.client_id == client_id, ClientCredential.kind == kind
        )
    )
    if row:
        row.content = content
        row.is_secret = is_secret
        row.serial = serial
        row.expires_at = expires_at
        row.revoked = False
    else:
        session.add(
            ClientCredential(
                client_id=client_id,
                kind=kind,
                content=content,
                is_secret=is_secret,
                serial=serial,
                expires_at=expires_at,
            )
        )


def revoke_client_certificate(session: OrmSession, inbound: Inbound, client: Client) -> None:
    rows = session.scalars(
        select(ClientCredential).where(ClientCredential.client_id == client.id)
    ).all()
    for row in rows:
        row.revoked = True
    material = ensure_material(session, inbound)
    material.crl = refresh_crl(session, inbound)
    _store_material(session, inbound.id, material)
    write_material_files(inbound, material, session=session)


def client_credentials(session: OrmSession, client_id: int) -> dict[str, str]:
    rows = session.scalars(
        select(ClientCredential).where(ClientCredential.client_id == client_id)
    ).all()
    return {row.kind: row.content for row in rows}


# ---------------------------------------------------------------------------
# Files and configuration rendering
# ---------------------------------------------------------------------------


def write_material_files(
    inbound: Inbound, material: Material, *, session: OrmSession | None = None
) -> Path:
    directory = paths.ensure(inbound_dir(inbound), 0o750)
    files: list[tuple[str, str, int]] = [
        ("ca.crt", material.ca_cert, 0o644),
        ("ca.key", material.ca_key, 0o600),
        ("server.crt", material.server_cert, 0o644),
        ("server.key", material.server_key, 0o600),
        ("ta.key", material.tls_crypt, 0o600),
    ]
    if material.crl:
        files.append(("crl.pem", material.crl, 0o644))
    else:
        files.append(("crl.pem", "", 0o644))

    for name, content, mode in files:
        if not content:
            with contextlib.suppress(OSError):
                (directory / name).unlink()
            continue
        shell.write_file_locked(directory / name, content, mode=mode)

    paths.ensure(directory / "ccd", 0o750)
    if session is not None:
        write_client_config_dir(session, inbound)
    return directory


def build_ccd_map(session: OrmSession, inbound: Inbound) -> dict[str, str]:
    """Map ``common_name -> ccd content`` for the inbound's clients.

    A /24 pool gets deterministic addresses so a client keeps the same tunnel
    IP across sessions; wider or narrower pools fall back to OpenVPN's own
    allocator and return an empty map.
    """
    settings = effective_settings(inbound)
    if not settings.get("static_addresses"):
        return {}

    network = inbound.network_cidr or "10.8.0.0/24"
    try:
        base, prefix = network.split("/")
        prefix_int = int(prefix)
    except ValueError:
        return {}
    if prefix_int != 24:
        return {}

    octets = base.split(".")
    if len(octets) != 4:
        return {}
    pool_prefix = ".".join(octets[:3])

    from ..models import ClientStatus

    clients = session.scalars(
        select(Client)
        .where(
            Client.inbound_id == inbound.id,
            Client.status != ClientStatus.EXPIRED.value,
        )
        .order_by(Client.id)
    ).all()

    mapping: dict[str, str] = {}
    # .1 is the server itself, so client addresses start at .2
    used: set[int] = {1}
    host = 2
    for client in clients:
        while host in used:
            host += 1
        if host > 254:
            log.warning("static address pool exhausted for inbound %s", inbound.tag)
            break
        used.add(host)
        mapping[client.common_name] = f"ifconfig-push {pool_prefix}.{host} 255.255.255.0\n"
        host += 1
    return mapping


def write_client_config_dir(session: OrmSession, inbound: Inbound) -> None:
    """Write the per-client ``ifconfig-push`` files next to the server config."""
    ccd_dir = inbound_dir(inbound) / "ccd"
    paths.ensure(ccd_dir, 0o750)
    desired = build_ccd_map(session, inbound)
    for name, content in desired.items():
        shell.write_file_locked(ccd_dir / name, content, mode=0o600)
    for stale in ccd_dir.glob("*"):
        if stale.is_file() and stale.name not in desired:
            with contextlib.suppress(OSError):
                stale.unlink()


def render_server_config(inbound: Inbound, *, session: OrmSession | None = None) -> str:
    settings = effective_settings(inbound)
    directory = inbound_dir(inbound)
    network = inbound.network_cidr or "10.8.0.0/24"
    try:
        base, _prefix = network.split("/")
    except ValueError as exc:
        raise OpenVPNError(f"invalid network {network!r}") from exc

    lines: list[str] = [
        "# Managed by Panah — do not edit by hand.",
        f"# inbound: {inbound.tag}",
        f"port {inbound.listen_port}",
        f"proto {'udp' if inbound.transport == 'udp' else 'tcp-server'}",
        f"local {inbound.listen_host or '0.0.0.0'}",
        "dev tun",
        "topology subnet",
        f"server {base} 255.255.255.0",
        f"ifconfig-pool-persist {paths.posix(directory / 'ipp.txt')}",
        f"client-config-dir {paths.posix(directory / 'ccd')}",
        f"max-clients {settings['max_clients']}",
        "mtu-disc yes",
        f"tun-mtu {min(int(settings['mtu']), DEFAULT_TUN_MTU)}",
        f"ca {paths.posix(directory / 'ca.crt')}",
        f"cert {paths.posix(directory / 'server.crt')}",
        f"key {paths.posix(directory / 'server.key')}",
        f"tls-crypt {paths.posix(directory / 'ta.key')}",
        "dh none",
        "ecdh-curve prime256v1",
        f"tls-version-min {settings['tls_version_min']}",
        "tls-cipher TLS-ECDHE-ECDSA-WITH-AES-256-GCM-SHA384:TLS-ECDHE-RSA-WITH-AES-256-GCM-SHA384",
        f"data-ciphers {settings['data_ciphers']}",
        f"data-ciphers-fallback {settings['data_ciphers_fallback']}",
        f"auth {settings['auth_digest']}",
        "remote-cert-tls client",
        "verify-client-cert require",
        "user nobody",
        "group nogroup",
        "persist-key",
        "persist-tun",
        "explicit-exit-notify 1",
        f"keepalive {settings['keepalive_interval']} {settings['keepalive_timeout']}",
        f"reneg-sec {settings['reneg_sec']}",
        "tls-server",
        "tls-timeout 2",
        "hand-window 120",
        f"txqueuelen {settings['txqueuelen']}",
        f"status {paths.posix(status_path(inbound.tag))} 5",
        "status-version 2",
        f"management {paths.posix(management_socket(inbound.tag))} unix",
        f"verb {settings['verbosity']}",
        "mute 20",
        f"log-append {paths.posix(paths.log_dir() / ('openvpn-' + paths.slugify(inbound.tag) + '.log'))}",
        f"crl-verify {paths.posix(directory / 'crl.pem')}"
        if (directory / "crl.pem").exists()
        else "",
    ]

    if settings.get("sndbuf"):
        lines.append(f"sndbuf {int(settings['sndbuf'])}")
    if settings.get("rcvbuf"):
        lines.append(f"rcvbuf {int(settings['rcvbuf'])}")

    # Compression is disabled by default: it is a known oracle risk (VORACLE)
    # and modern clients do not benefit from it on already-encrypted traffic.
    if settings.get("compress") not in (None, "", "none"):
        lines.append(f"compress {settings['compress']}")

    if settings.get("duplicate_cn"):
        lines.append("duplicate-cn")

    if settings.get("redirect_gateway", True):
        lines.append('push "redirect-gateway def1 bypass-dhcp"')

    for server in settings.get("dns_servers") or []:
        lines.append(f'push "dhcp-option DNS {server}"')

    for route in settings.get("push_routes") or []:
        pushed = _route_push(str(route))
        if pushed:
            lines.append(pushed)

    # DPI evasion: hand TCP noise to the fake website instead of resetting.
    if settings.get("port_share") and inbound.transport == "tcp":
        host, _, port = str(settings.get("port_share_target", "127.0.0.1:8443")).rpartition(":")
        lines.append(f"port-share {host} {port}")

    cleaned = [line for line in lines if line and line.strip()]
    return "\n".join(cleaned) + "\n"


def render_systemd_unit(inbound: Inbound, *, binary: str = "/usr/sbin/openvpn") -> str:
    directory = inbound_dir(inbound)
    return (
        "[Unit]\n"
        f"Description=Panah OpenVPN inbound {inbound.tag}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "PartOf=panah-openvpn.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "LimitNPROC=100\n"
        "LimitNOFILE=1000000\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "RuntimeDirectory=panah\n"
        "RuntimeDirectoryMode=0755\n"
        f"ExecStartPre=/bin/mkdir -p {paths.posix(directory / 'ccd')}\n"
        f"ExecStart={binary} --suppress-timestamps --config "
        f"{paths.posix(directory / 'server.conf')}\n"
        "WorkingDirectory=/\n"
        "CapabilityBoundingSet=CAP_IPC_LOCK CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW "
        "CAP_SETGID CAP_SETUID CAP_SYS_CHROOT CAP_DAC_OVERRIDE\n"
        "AmbientCapabilities=CAP_IPC_LOCK CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW\n"
        "PrivateTmp=true\n"
        "ProtectSystem=full\n"
        "ProtectHome=true\n"
        "NoNewPrivileges=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def render_client_config(
    inbound: Inbound,
    client: Client,
    *,
    credentials: dict[str, str],
    material: Material,
    remote_host: str,
    remote_port: int | None = None,
    protocol: str | None = None,
) -> str:
    """Full single-file ``.ovpn`` with inline certificates."""
    settings = effective_settings(inbound)
    lines = [
        "client",
        "dev tun",
        f"proto {'udp' if (protocol or inbound.transport) == 'udp' else 'tcp-client'}",
        f"remote {remote_host} {remote_port or inbound.listen_port}",
        "resolv-retry infinite",
        "nobind",
        "persist-key",
        "persist-tun",
        "remote-cert-tls server",
        "verify-x509-name server-" + inbound.tag + " name-prefix",
        "tls-version-min 1.2",
        f"data-ciphers {settings['data_ciphers']}",
        f"data-ciphers-fallback {settings['data_ciphers_fallback']}",
        f"auth {settings['auth_digest']}",
        "compress stub-v2",
        "verb 3",
        "pull",
        "auth-nocache",
        "<ca>",
        material.ca_cert.strip(),
        "</ca>",
        "<cert>",
        credentials.get("cert", "").strip(),
        "</cert>",
        "<key>",
        credentials.get("key", "").strip(),
        "</key>",
        "<tls-crypt>",
        material.tls_crypt.strip(),
        "</tls-crypt>",
    ]
    return "\n".join(line for line in lines if line is not None) + "\n"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def apply_inbound(session: OrmSession, inbound: Inbound, *, restart: bool = True) -> Capabilities:
    """Materialise files, reload systemd and (re)start the inbound unit."""
    caps = detect()
    if not caps.systemd:
        raise OpenVPNError("systemd is unavailable; cannot manage OpenVPN inbounds")

    _run_dir()
    material = ensure_material(session, inbound)
    paths.ensure(inbound_dir(inbound), 0o750)
    write_material_files(inbound, material, session=session)
    shell.write_file_locked(
        inbound_dir(inbound) / "server.conf",
        render_server_config(inbound, session=session),
        mode=0o600,
    )
    unit_path = Path("/etc/systemd/system") / unit_name(inbound.tag)
    binary = caps.binary or "/usr/sbin/openvpn"
    shell.write_file_locked(unit_path, render_systemd_unit(inbound, binary=binary), mode=0o644)
    shell.run(["systemctl", "daemon-reload"])
    action = "restart" if restart else "start"
    result = shell.run(["systemctl", action, unit_name(inbound.tag)], timeout=60)
    if not result.ok:
        raise OpenVPNError(f"systemctl {action} failed: {result.message}")
    return caps


def stop_inbound(inbound: Inbound) -> None:
    shell.run(["systemctl", "stop", unit_name(inbound.tag)], timeout=60)
    shell.run(["systemctl", "disable", unit_name(inbound.tag)], timeout=60)


def remove_inbound(inbound: Inbound) -> None:
    """Stop the unit and delete every generated artifact."""
    caps = detect()
    if caps.systemd:
        stop_inbound(inbound)
    unit_path = Path("/etc/systemd/system") / unit_name(inbound.tag)
    with contextlib.suppress(OSError):
        unit_path.unlink()
    if caps.systemd:
        shell.run(["systemctl", "daemon-reload"])

    directory = inbound_dir(inbound)
    if directory.exists():
        shell.run(["rm", "-rf", str(directory)], allow_unlisted=True)
    for path in (status_path(inbound.tag), management_socket(inbound.tag)):
        with contextlib.suppress(OSError):
            path.unlink()


def reload_inbound(session: OrmSession, inbound: Inbound) -> None:
    """Push a changed config without dropping established clients when possible."""
    write_material_files(inbound, ensure_material(session, inbound), session=session)
    shell.write_file_locked(
        inbound_dir(inbound) / "server.conf",
        render_server_config(inbound, session=session),
        mode=0o600,
    )
    shell.run(["systemctl", "reload-or-restart", unit_name(inbound.tag)], timeout=60)


def unit_state(tag: str) -> tuple[bool, str]:
    result = shell.run(["systemctl", "is-active", unit_name(tag)], timeout=10)
    state = result.output or "unknown"
    return state == "active", state


# ---------------------------------------------------------------------------
# Status and accounting
# ---------------------------------------------------------------------------


def parse_status(text: str) -> list[Session]:
    """Parse ``status-version 1/2/3`` output.

    The column layout differs between versions (version 3 adds a "Virtual IPv6
    Address" field). When OpenVPN prints the ``CLIENT_LIST`` header we use it
    directly; otherwise the byte counters are located by shape so the counters
    are never silently swapped.
    """
    sessions: list[Session] = []
    header: list[str] | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            continue
        fields = line.split("\t") if "\t" in line else line.split(",")
        if len(fields) < 2:
            continue
        keyword = fields[0].strip()
        if keyword == "CLIENT_LIST":
            columns = [c.strip() for c in fields[1:]]
            if columns and columns[0].lower().startswith("common name"):
                header = columns
            else:
                sessions.append(_session_from(fields[1:], header))
        elif keyword == "Common Name" and fields[1].strip().lower().startswith("real"):
            # status-version 1 prints a bare header row.
            header = [keyword] + [c.strip() for c in fields[1:]]
        elif keyword == "END":
            break
    return sessions


def _session_from(values: list[str], header: list[str] | None) -> Session:
    clean = [value.strip() for value in values]

    if header:
        mapping = {column.lower(): index for index, column in enumerate(header)}

        def pick(name: str, default_index: int | None = None) -> str:
            index = mapping.get(name)
            if index is None:
                index = default_index
            if index is not None and index < len(clean):
                return clean[index]
            return ""
    else:
        # Locate the two adjacent integer fields; they are always
        # ``Bytes Received`` / ``Bytes Sent`` and appear before timestamps.
        counter_index = None
        for index in range(2, len(clean) - 1):
            if clean[index].isdigit() and clean[index + 1].isdigit():
                counter_index = index
                break
        if counter_index is None:
            counter_index = 4 if len(clean) > 6 else 3

        def pick(name: str, default_index: int | None = None) -> str:
            index = default_index
            if name == "bytes received":
                index = counter_index
            elif name == "bytes sent":
                index = counter_index + 1
            elif name == "connected since":
                index = counter_index + 2
            if index is not None and index < len(clean):
                return clean[index]
            return ""

    def as_int(value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return Session(
        common_name=pick("common name", 0),
        real_address=pick("real address", 1),
        virtual_address=pick("virtual address", 2),
        bytes_received=as_int(pick("bytes received", None)),
        bytes_sent=as_int(pick("bytes sent", None)),
        connected_since=_parse_time(pick("connected since", None)),
        username=pick("username", None),
        client_id=pick("client id", None),
    )


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    # status-version 2 uses "Mon Jan  2 15:04:05 2006".
    for fmt in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def read_status(tag: str) -> list[Session]:
    path = status_path(tag)
    try:
        return parse_status(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        log.debug("no status file for %s (%s)", tag, exc)
        return []


# ---------------------------------------------------------------------------
# Management interface
# ---------------------------------------------------------------------------


class ManagementClient:
    """Minimal client for OpenVPN's unix management socket."""

    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def command(self, command: str) -> str:
        if not self.socket_path.exists():
            raise OpenVPNError(f"management socket {self.socket_path} is absent")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            try:
                sock.connect(str(self.socket_path))
            except OSError as exc:
                raise OpenVPNError(f"cannot reach management socket: {exc}") from exc
            response = sock.recv(65536).decode("utf-8", errors="replace")
            sock.sendall((command + "\n").encode("utf-8"))
            chunks: list[str] = []
            deadline = self.timeout
            while deadline > 0:
                try:
                    data = sock.recv(65536)
                except TimeoutError:
                    break
                if not data:
                    break
                chunks.append(data.decode("utf-8", errors="replace"))
                if "END" in chunks[-1] or "SUCCESS" in chunks[-1] or "ERROR" in chunks[-1]:
                    break
                deadline -= 0.5
        return response + "".join(chunks)

    def kill(self, common_name: str) -> bool:
        output = self.command(f"kill {common_name}")
        return "SUCCESS" in output or "error" not in output.lower()

    def status(self) -> str:
        return self.command("status 2")


def kill_session(tag: str, common_name: str) -> bool:
    """Forcibly disconnect a client (used when a quota or expiry trips)."""
    try:
        return ManagementClient(management_socket(tag)).kill(common_name)
    except OpenVPNError as exc:
        log.warning("could not kill %s on %s: %s", common_name, tag, exc)
        return False


# ---------------------------------------------------------------------------
# Convenience wrappers used by the API and scheduler
# ---------------------------------------------------------------------------


def sessions_for(inbound: Inbound) -> list[Session]:
    return read_status(inbound.tag)


def inbound_usage(inbound: Inbound) -> tuple[int, int, int]:
    sessions = sessions_for(inbound)
    up = sum(s.bytes_sent for s in sessions)
    down = sum(s.bytes_received for s in sessions)
    return up, down, len(sessions)


def summarize(sessions: Iterable[Session]) -> dict[str, Any]:
    sessions = list(sessions)
    return {
        "connections": len(sessions),
        "bytes_received": sum(s.bytes_received for s in sessions),
        "bytes_sent": sum(s.bytes_sent for s in sessions),
    }
