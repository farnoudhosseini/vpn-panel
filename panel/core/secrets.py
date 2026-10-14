"""Secret material loading and generation.

Secrets are never stored in the repository. The installer writes them to
``/etc/panah/secrets`` (mode 0600) and this module either reads that directory
or generates the files on first boot. Environment variables always win, which
keeps containerised deployments straightforward.
"""

from __future__ import annotations

import base64
import os
import secrets
import stat
from pathlib import Path

from .logging import get_logger

log = get_logger(__name__)

SECRETS_DIR_ENV = "PANAH_SECRETS_DIR"
DEFAULT_SECRETS_DIR = Path("/etc/panah/secrets")

# name -> number of random bytes
_GENERATED = {
    "secret_key": 48,
    "encryption_key": 32,
    "agent_key": 32,
}


def secrets_dir() -> Path:
    return Path(os.environ.get(SECRETS_DIR_ENV) or DEFAULT_SECRETS_DIR)


def _write_locked(path: Path, value: str) -> None:
    """Write ``value`` to ``path`` atomically with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Create with restrictive permissions before writing any bytes.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
    finally:
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - best effort on exotic filesystems
            pass
    os.replace(tmp, path)


def _generate(name: str) -> str:
    raw = secrets.token_bytes(_GENERATED[name])
    if name == "secret_key":
        return raw.hex()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def load_secret(name: str, *, create: bool = True) -> str | None:
    """Return the secret called ``name``, generating it when missing.

    Returns ``None`` only when the value is absent and ``create`` is false.
    """
    if name not in _GENERATED:
        raise KeyError(f"unknown secret: {name}")

    env_name = f"PANAH_{name.upper()}"
    from_env = os.environ.get(env_name)
    if from_env:
        return from_env.strip()

    path = secrets_dir() / name
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
        log.warning("secret file %s is empty; regenerating", path)

    if not create:
        return None

    value = _generate(name)
    try:
        _write_locked(path, value)
        log.info("generated new %s at %s", name, path)
    except OSError as exc:  # pragma: no cover - read-only filesystem
        log.error("could not persist %s (%s); value is in-memory only", name, exc)
    return value
