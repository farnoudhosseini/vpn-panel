"""Shared fixtures.

Every test runs against a throwaway data directory: the panel keeps state in
files (SQLite, PKI, generated configs) and the suite must never touch a real
installation.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SUBDIRS = (
    "data",
    "logs",
    "secrets",
    "etc",
    "openvpn",
    "nginx-sites",
    "nginx-enabled",
    "backpack",
    "wireguard",
    "www",
    "run",
)


@pytest.fixture(scope="session", autouse=True)
def sandbox() -> Iterator[Path]:
    """Point every Panah path at a disposal root for the whole session.

    The roots are written in POSIX form on purpose: Panah always manages Linux
    hosts, and the plan validator refuses anything that is not an absolute
    POSIX path. Using the same shape everywhere keeps the suite honest about
    what a node will actually accept.
    """
    root = f"/tmp/panah-tests-{uuid4().hex[:8]}"
    os.environ.update(
        {
            "PANAH_DATA_DIR": f"{root}/data",
            "PANAH_LOG_DIR": f"{root}/logs",
            "PANAH_SECRETS_DIR": f"{root}/secrets",
            "PANAH_CONFIG_DIR": f"{root}/etc",
            "PANAH_OPENVPN_DIR": f"{root}/openvpn",
            "PANAH_NGINX_SITES": f"{root}/nginx-sites",
            "PANAH_NGINX_ENABLED": f"{root}/nginx-enabled",
            "PANAH_BACKPACK_DIR": f"{root}/backpack",
            "PANAH_WIREGUARD_DIR": f"{root}/wireguard",
            "PANAH_FAKE_SITE_ROOT": f"{root}/www",
            "PANAH_RUN_DIR": f"{root}/run",
            "PANAH_MODE": "panel",
            "PANAH_DOMAIN": "panel.test",
            "PANAH_COOKIE_SECURE": "0",
            "PANAH_ALLOWED_ORIGINS": "https://panel.test",
            "PANAH_SECRET_KEY": "test-secret-key-that-is-long-enough-for-hs256",
            "PANAH_AGENT_KEY": "test-agent-key",
            # The HTTP tests all share one client address; throttling itself is
            # covered directly in test_ratelimit.py.
            "PANAH_RATE_LIMIT_PER_MINUTE": "100000",
        }
    )
    for name in SUBDIRS:
        Path(f"{root}/{name}").mkdir(parents=True, exist_ok=True)

    from panel.api import deps
    from panel.config import reload_settings
    from panel.core.ratelimit import FailureTracker, RateLimiter

    reload_settings()

    # Every HTTP test signs in from the same address, so the per-IP limits are
    # lifted here. The limiter logic itself is covered in test_ratelimit.py.
    deps.request_limiter = RateLimiter(limit=1_000_000, window_seconds=60)
    deps.login_limiter = RateLimiter(limit=1_000_000, window_seconds=60)
    deps.failures = FailureTracker(threshold=1_000_000, lock_seconds=60)

    yield Path(root)

    # Leave no global state behind for the next session.
    shuffle = Path(root)
    with contextlib.suppress(OSError):
        shutil.rmtree(shuffle, ignore_errors=True)
    reload_settings()


@pytest.fixture(autouse=True)
def isolated_database(sandbox: Path) -> Iterator[Path]:
    """Give every test its own database file.

    The engine is process-wide, so the cached engine is dropped between tests;
    without this, a test that commits (the API ones do) leaves rows behind and
    the next test trips over unique constraints or missing foreign keys.
    """
    from panel import db as db_module
    from panel.config import reload_settings

    path = sandbox / "data" / f"test-{uuid4().hex[:8]}.db"
    os.environ["PANAH_DB_PATH"] = str(path)
    reload_settings()
    _reset_engine(db_module)
    yield path
    _reset_engine(db_module)
    os.environ.pop("PANAH_DB_PATH", None)
    reload_settings()


def _reset_engine(db_module) -> None:
    engine = db_module._engine
    if engine is not None:
        with contextlib.suppress(Exception):
            engine.dispose()
    db_module._engine = None
    db_module._SessionLocal = None


@pytest.fixture()
def settings(sandbox: Path):
    from panel.config import reload_settings

    return reload_settings()


@pytest.fixture()
def session(sandbox: Path):
    """A database session on a fresh schema that is rolled back afterwards.

    Tests share one database file, so anything they write must never survive
    the test that wrote it.
    """
    from panel.db import get_sessionmaker, init_engine, run_migrations

    engine = init_engine()
    run_migrations(engine)
    active = get_sessionmaker()()
    try:
        yield active
    finally:
        active.rollback()
        active.close()


@pytest.fixture()
def client(sandbox: Path):
    """An authenticated HTTP client for the panel API."""
    from fastapi.testclient import TestClient

    from panel.config import reload_settings
    from panel.core.security import hash_password
    from panel.db import init_engine, run_migrations, session_scope
    from panel.main import create_app
    from panel.models import Node, Role, User

    reload_settings()
    engine = init_engine()
    run_migrations(engine)

    password = "Test-Admin-Password-1!"
    with session_scope() as active:
        from sqlalchemy import select

        if active.scalar(select(User).where(User.username == "tester")) is None:
            active.add(
                User(
                    username="tester",
                    password_hash=hash_password(password),
                    role=Role.ADMIN.value,
                    is_active=True,
                    must_change_password=False,
                )
            )
        if active.scalar(select(Node).where(Node.is_local.is_(True))) is None:
            active.add(Node(name="local", address="127.0.0.1", is_local=True, enabled=True))

    app = create_app()
    with TestClient(app) as http:
        response = http.post(
            "/api/auth/login", json={"username": "tester", "password": password}
        )
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        http.headers.update({"Authorization": f"Bearer {token}"})
        yield http
