"""Nginx front end.

Two jobs:

* **SNI sharing** — many inbounds can listen behind one port. The ``stream``
  module reads the TLS Client Hello, picks a backend by server name, and sends
  anything unmatched to the decoy website.
* **Decoy website** — a static site on the same port so a probe or a browser
  visit looks like an ordinary web server instead of a VPN listener.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..models import Inbound
from . import paths, shell

log = get_logger("nginx")

STREAM_CONF = Path("/etc/nginx/stream.d")
HTTP_CONF = Path("/etc/nginx/conf.d")
SNIPPET_MARKER = "# Managed by Panah"


@dataclass(slots=True)
class Upstream:
    name: str
    host: str
    port: int


@dataclass(slots=True)
class Route:
    """One SNI rule mapping a hostname to a local backend."""

    server_name: str
    upstream: Upstream
    proxy_protocol: bool = False


def capabilities() -> dict[str, Any]:
    installed = shell.available("nginx")
    has_stream = False
    version = None
    if installed:
        result = shell.run(["nginx", "-V"], timeout=10)
        text = f"{result.stdout}\n{result.stderr}"
        has_stream = "with-stream" in text and "with-stream_ssl_preread_module" in text
        import re

        match = re.search(r"nginx/([0-9.]+)", text)
        version = match.group(1) if match else None
    return {
        "installed": installed,
        "version": version,
        "stream_module": has_stream,
        "detail": "" if has_stream else "nginx stream/ssl_preread module unavailable",
    }


def _site_path(inbound: Inbound) -> Path:
    return paths.nginx_sites_dir() / f"panah-{paths.slugify(inbound.tag)}.conf"


def _enabled_path(inbound: Inbound) -> Path:
    return paths.nginx_enabled_dir() / f"panah-{paths.slugify(inbound.tag)}.conf"


def render_decoy_site(
    *,
    title: str,
    domain: str,
    body_html: str | None = None,
    listen_ports: Sequence[int] = (80,),
) -> str:
    """A small, believable static site used as the port-share fallback."""
    safe_title = (title or "Welcome").replace("<", "").replace(">", "")[:120]
    content = body_html or _default_body(safe_title, domain)
    listen_lines = "\n".join(f"    listen {port} default_server;" for port in listen_ports)
    return (
        f"{SNIPPET_MARKER}\n"
        "server {\n"
        f"{listen_lines}\n"
        f"    server_name {domain} _;\n"
        "    root /var/www/panah/decoy;\n"
        "    index index.html;\n"
        "    access_log off;\n"
        "    location / {\n"
        "        try_files $uri $uri/ /index.html;\n"
        "    }\n"
        "    location = /healthz {\n"
        "        return 200 'ok';\n"
        "        add_header Content-Type text/plain;\n"
        "    }\n"
        "}\n"
        f"{content}"
    )


def _default_body(title: str, domain: str) -> str:
    # Kept as a comment so the site stays a single static file.
    return (
        f"# decoy website for {domain}\n"
        f"# title: {title}\n"
    )


def write_decoy_index(title: str, *, body: str | None = None) -> Path:
    """Write the static decoy page and return its path."""
    root = paths.ensure(paths.fake_site_root() / "decoy", 0o755)
    safe_title = (title or "Home").replace("<", "").replace(">", "")[:120]
    html = body or _default_index_html(safe_title)
    index = root / "index.html"
    shell.write_file_locked(index, html, mode=0o644)
    return index


def _default_index_html(title: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ margin: 0; font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
           display: grid; place-items: center; min-height: 100vh; background: #f6f7f9; color: #1c1f24; }}
    main {{ max-width: 34rem; padding: 3rem 2rem; }}
    h1 {{ font-size: 1.65rem; margin: 0 0 .75rem; letter-spacing: -.01em; }}
    p {{ margin: 0 0 .5rem; color: #4a5160; }}
    footer {{ margin-top: 2rem; font-size: .8rem; color: #8b93a1; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p>This server is operating normally.</p>
    <p>Content is served from the default site configuration.</p>
    <footer>nginx</footer>
  </main>
</body>
</html>
"""


def render_stream_router(
    *,
    listen_port: int,
    routes: Sequence[Route],
    fallback_host: str = "127.0.0.1",
    fallback_port: int = 80,
    proxy_protocol: bool = False,
) -> str:
    """SNI-based TCP router for ``listen_port``.

    Decrypted-free inspection (``ssl_preread``) keeps the TLS handshake
    end-to-end between client and backend while still allowing many hostnames
    on one port.
    """
    lines = [
        f"{SNIPPET_MARKER} stream router on {listen_port}",
        "map $ssl_preread_server_name $panah_backend {",
        "    default fallback;",
    ]
    for route in routes:
        name = route.server_name.strip()
        if name:
            lines.append(f"    {name} {route.upstream.name};")
    lines.append("}")
    lines.append("")

    for route in routes:
        lines.extend(
            [
                f"upstream {route.upstream.name} {{",
                f"    server {route.upstream.host}:{route.upstream.port};",
                "}",
            ]
        )
    lines.extend(
        [
            "upstream fallback {",
            f"    server {fallback_host}:{fallback_port};",
            "}",
            "",
            "server {",
            f"    listen {listen_port} reuseport;",
            "    proxy_pass $panah_backend;",
            "    ssl_preread on;",
            "    proxy_connect_timeout 5s;",
            "    proxy_timeout 24h;",
            "}",
        ]
    )
    if proxy_protocol or any(route.proxy_protocol for route in routes):
        # Only enable when every backend speaks PROXY protocol, otherwise the
        # header corrupts the stream.
        lines.insert(len(lines) - 1, "    proxy_protocol on;")
    return "\n".join(lines) + "\n"


def render_decoy_https(
    *,
    domain: str,
    listen_port: int,
    certificate: Path | None,
    key: Path | None,
) -> str:
    """HTTPS decoy site used when a real certificate exists."""
    if not certificate or not key:
        return ""
    return (
        f"{SNIPPET_MARKER} https decoy for {domain}\n"
        "server {\n"
        f"    listen {listen_port} ssl http2;\n"
        f"    server_name {domain};\n"
        f"    ssl_certificate {paths.posix(certificate)};\n"
        f"    ssl_certificate_key {paths.posix(key)};\n"
        "    ssl_protocols TLSv1.2 TLSv1.3;\n"
        "    ssl_session_cache shared:SSL:10m;\n"
        "    root /var/www/panah/decoy;\n"
        "    index index.html;\n"
        "}\n"
    )


def install_stream_router(name: str, content: str) -> Path:
    """Write a stream router into ``stream.d`` and include it from nginx.conf."""
    paths.ensure(STREAM_CONF, 0o755)
    target = STREAM_CONF / f"panah-{paths.slugify(name)}.conf"
    shell.write_file_locked(target, content, mode=0o644)
    _ensure_include(STREAM_CONF / "panah-*.conf")
    return target


def remove_stream_router(name: str) -> None:
    target = STREAM_CONF / f"panah-{paths.slugify(name)}.conf"
    with contextlib.suppress(OSError):
        target.unlink()


def install_site(inbound: Inbound, content: str) -> Path:
    paths.ensure(paths.nginx_sites_dir(), 0o755)
    paths.ensure(paths.nginx_enabled_dir(), 0o755)
    target = _site_path(inbound)
    shell.write_file_locked(target, content, mode=0o644)
    link = _enabled_path(inbound)
    with contextlib.suppress(FileNotFoundError, OSError):
        if link.is_symlink() or link.exists():
            link.unlink()
    try:
        link.symlink_to(target)
    except OSError:  # pragma: no cover - filesystem without symlinks
        shell.write_file_locked(link, content, mode=0o644)
    return target


def remove_site(inbound: Inbound) -> None:
    for path in (_enabled_path(inbound), _site_path(inbound)):
        with contextlib.suppress(OSError):
            path.unlink()


def _ensure_include(pattern: Path) -> None:
    """Make sure ``nginx.conf`` includes our stream snippet directory."""
    main = Path("/etc/nginx/nginx.conf")
    if not main.exists():
        return
    try:
        text = main.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover
        return
    needle = f"include {pattern};"
    if needle in text:
        return
    block = (
        f"\n# Panah stream routers\n"
        f"stream {{\n"
        f"    include {pattern};\n"
        f"}}\n"
    )
    try:
        main.write_text(text.rstrip() + block, encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        log.warning("could not add stream include to nginx.conf: %s", exc)


def test_configuration() -> tuple[bool, str]:
    result = shell.run(["nginx", "-t"], timeout=20)
    output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
    return result.ok, output


def reload_nginx() -> tuple[bool, str]:
    ok, output = test_configuration()
    if not ok:
        return False, output
    result = shell.run(["systemctl", "reload", "nginx"], timeout=30)
    if not result.ok:
        result = shell.run(["systemctl", "restart", "nginx"], timeout=30)
    return result.ok, result.message


def status() -> dict[str, Any]:
    result = shell.run(["systemctl", "is-active", "nginx"], timeout=10)
    return {"active": result.output == "active", "state": result.output or "unknown"}
