"""Canonical on-disk layout.

Every path the panel writes lives below one of these roots so packagers and the
uninstaller can reason about the footprint. Environment overrides exist for
development checkouts and tests.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import get_settings


def _env_path(name: str, default: str) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else Path(default)


def config_dir() -> Path:
    return _env_path("PANAH_CONFIG_DIR", "/etc/panah")


def secrets_dir() -> Path:
    return Path(os.environ.get("PANAH_SECRETS_DIR") or (config_dir() / "secrets"))


def run_dir() -> Path:
    return _env_path("PANAH_RUN_DIR", "/run/panah")


def openvpn_root() -> Path:
    return _env_path("PANAH_OPENVPN_DIR", "/etc/openvpn/panah")


def nginx_sites_dir() -> Path:
    return _env_path("PANAH_NGINX_SITES", "/etc/nginx/sites-available")


def nginx_enabled_dir() -> Path:
    return _env_path("PANAH_NGINX_ENABLED", "/etc/nginx/sites-enabled")


def backpack_dir() -> Path:
    return _env_path("PANAH_BACKPACK_DIR", "/etc/backpack")


def wireguard_dir() -> Path:
    return _env_path("PANAH_WIREGUARD_DIR", "/etc/wireguard")


def fake_site_root() -> Path:
    return _env_path("PANAH_FAKE_SITE_ROOT", "/var/www/panah")


def data_dir() -> Path:
    return get_settings().data_dir


def pki_dir() -> Path:
    return data_dir() / "pki"


def clients_dir() -> Path:
    return data_dir() / "clients"


def generated_dir() -> Path:
    return data_dir() / "generated"


def log_dir() -> Path:
    return get_settings().log_dir


def ensure(path: Path, mode: int = 0o750) -> Path:
    """Create ``path`` (and parents) and tighten permissions where possible."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError:  # pragma: no cover - FAT/NTFS and containers
        pass
    return path


def posix(path: str | Path) -> str:
    """Render a path the way the target host expects it.

    Panah always manages Linux hosts, and a plan may be rendered on a machine
    whose path separator differs (a developer's Windows checkout). Embedding
    the POSIX form keeps generated configs and systemd units correct
    regardless of where they were rendered.
    """
    return str(path).replace("\\", "/")


def slugify(value: str, *, max_length: int = 40) -> str:
    """Filesystem and systemd friendly identifier."""
    cleaned = [c.lower() if c.isalnum() else "-" for c in value.strip()]
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug or "item"
    return slug[:max_length].strip("-") or "item"
