"""Typed application settings.

Everything is driven by environment variables with the ``PANAH_`` prefix so the
same code runs as a full panel, a lightweight node agent, or a developer
checkout. Secrets are read through :mod:`panel.core.secrets` and are never
committed to the repository.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .core.secrets import load_secret

Mode = Literal["panel", "node"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PANAH_",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- identity ---------------------------------------------------------
    mode: Mode = "panel"
    version: str = "1.0.0"

    # -- paths ------------------------------------------------------------
    data_dir: Path = Path("/opt/panah/data")
    log_dir: Path = Path("/var/log/panah")
    db_path: Path | None = None

    # -- networking -------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8080
    agent_port: int = 8787
    domain: str = "localhost"
    allowed_origins: str = ""

    # -- session policy ---------------------------------------------------
    access_token_minutes: int = 30
    refresh_token_days: int = 7
    cookie_secure: bool = True

    # -- bootstrap --------------------------------------------------------
    admin_username: str = "admin"
    admin_password: str | None = None

    # -- hardening --------------------------------------------------------
    max_login_attempts: int = 5
    login_lock_seconds: int = 900
    rate_limit_per_minute: int = 240
    ip_allowlist: str = ""
    trusted_proxies: str = "127.0.0.1,::1"

    # -- log level --------------------------------------------------------
    log_level: str = "INFO"

    # Secrets are resolved lazily so importing settings never writes to disk.
    secret_key: str = Field(default="", repr=False)
    agent_key: str = Field(default="", repr=False)

    @field_validator("port", "agent_port")
    @classmethod
    def _valid_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return value

    # -- derived helpers --------------------------------------------------
    @property
    def database_path(self) -> Path:
        if self.db_path:
            return self.db_path
        return self.data_dir / "panah.db"

    @property
    def allowed_origin_list(self) -> list[str]:
        raw = self.allowed_origins.strip()
        if not raw:
            return [f"https://{self.domain}"] if self.domain != "localhost" else []
        return [item.strip() for item in raw.split(",") if item.strip()]

    @property
    def allowed_origin_list_fallback(self) -> list[str]:
        origins = self.allowed_origin_list
        for candidate in (f"https://{self.domain}", f"http://{self.domain}"):
            if candidate not in origins:
                origins.append(candidate)
        return origins

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_proxies.split(",") if item.strip()]

    @property
    def ip_allowlist_entries(self) -> list[str]:
        return [item.strip() for item in self.ip_allowlist.split(",") if item.strip()]

    def resolve_secrets(self) -> None:
        """Populate secret fields from the environment or the secrets dir."""
        if self.mode == "node":
            # The agent only needs the shared key it validates.
            self.agent_key = self.agent_key or (load_secret("agent_key") or "")
            return
        self.secret_key = self.secret_key or (load_secret("secret_key") or "")
        self.agent_key = self.agent_key or (load_secret("agent_key") or "")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.data_dir.chmod(0o750)
        except OSError:  # pragma: no cover - best effort
            pass
        self.log_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.resolve_secrets()
    settings.ensure_dirs()
    return settings


def reload_settings() -> Settings:
    """Drop the cache — used by tests and by the CLI after writing config."""
    get_settings.cache_clear()
    return get_settings()


def settings_for_repr() -> dict[str, object]:
    """Safe dump for debug endpoints (never contains secret values)."""
    settings = get_settings()
    data = settings.model_dump()
    for key in ("secret_key", "agent_key", "admin_password"):
        if data.get(key):
            data[key] = "***"
    data["db_path"] = str(settings.database_path)
    data["pid"] = os.getpid()
    return data
