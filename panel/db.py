"""Database engine, session management and migrations.

SQLite is tuned for a write-light control plane: WAL journalling, a larger page
cache, memory temp store and a busy timeout so the background accounting task
never trips over a concurrent API write.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .core.logging import get_logger

log = get_logger("db")

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None

# PRAGMAs applied to every new connection, in order. ``busy_timeout`` must
# come first so the later statements wait for a concurrent writer instead of
# failing immediately with "database is locked".
#
# ``PRAGMA optimize`` is deliberately absent here: it needs a write lock and
# running it on every connection makes the pool contend with itself. It runs
# once after migrations and then from the maintenance job instead.
_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("busy_timeout", "8000"),
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("cache_size", "-65536"),
    ("temp_store", "MEMORY"),
    ("mmap_size", "268435456"),
    ("wal_autocheckpoint", "1000"),
    ("recursive_triggers", "ON"),
)


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for name, value in _PRAGMAS:
            try:
                cursor.execute(f"PRAGMA {name}={value}")
            except Exception:  # pragma: no cover - e.g. WAL unsupported
                log.debug("PRAGMA %s=%s not supported", name, value)
    finally:
        cursor.close()


def _build_engine(db_path: Path) -> Engine:
    from sqlalchemy import create_engine

    in_memory = str(db_path) == ":memory:"
    url = "sqlite+pysqlite:///:memory:" if in_memory else f"sqlite+pysqlite:///{db_path}"
    if not in_memory:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        future=True,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 20},
        pool_pre_ping=True,
        **({} if in_memory else {"pool_size": 8, "max_overflow": 8}),
    )
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def init_engine(db_path: Path | None = None) -> Engine:
    """Create the process-wide engine (idempotent)."""
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine
    path = db_path or get_settings().database_path
    _engine = _build_engine(path)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    return _engine or init_engine()


def get_sessionmaker() -> sessionmaker[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on failure."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency.

    The transaction is committed only when the request handler finishes
    successfully. Any exception — including an ``HTTPException`` raised after a
    partial write — rolls the whole request back, so a failed operation can
    never leave half-applied state behind.
    """
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    else:
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------

MIGRATIONS: list[tuple[int, str, Any]] = []


def migration(version: int, description: str):
    def decorator(func: Any) -> Any:
        MIGRATIONS.append((version, description, func))
        return func

    return decorator


def _columns(conn: Any, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


@migration(1, "initial schema")
def _migration_initial(session: Session) -> None:
    """Baseline: the declarative metadata already created every table."""
    return None


@migration(2, "node capability cache and TLS pinning")
def _migration_node_columns(session: Session) -> None:
    """Add columns introduced after the first release.

    ``create_all`` already covers fresh databases, so each column is added only
    when it is genuinely missing; this also makes the migration safe to re-run.
    """
    conn = session.connection()
    existing = _columns(conn, "nodes")
    additions = {
        "tls_cert": "TEXT",
        "capabilities": "JSON",
    }
    for column, ddl in additions.items():
        if column in existing:
            continue
        log.info("adding nodes.%s", column)
        conn.exec_driver_sql(f"ALTER TABLE nodes ADD COLUMN {column} {ddl}")


@migration(3, "per-session traffic counters")
def _migration_traffic_counters(session: Session) -> None:
    """Create the counter table on databases that predate it.

    ``create_all`` has already created it for a fresh database, so the DDL is
    written defensively.
    """
    conn = session.connection()
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS traffic_counters ("
        " id INTEGER PRIMARY KEY,"
        " node_id INTEGER NOT NULL,"
        " inbound_tag VARCHAR(80) NOT NULL,"
        " common_name VARCHAR(80) NOT NULL,"
        " raw_received INTEGER NOT NULL DEFAULT 0,"
        " raw_sent INTEGER NOT NULL DEFAULT 0,"
        " updated_at DATETIME)"
    )
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_counter_session"
        " ON traffic_counters (node_id, inbound_tag, common_name)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_traffic_counters_node_id ON traffic_counters (node_id)"
    )


def current_version(session: Session) -> int:
    try:
        row = session.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar()
    except Exception:
        return 0
    return int(row or 0)


def run_migrations(engine: Engine | None = None) -> int:
    """Apply pending migrations and return the resulting schema version."""
    from .models import Base

    engine = engine or get_engine()
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY,"
                " description TEXT NOT NULL,"
                " applied_at TEXT NOT NULL)"
            )
        )

    applied: int
    with session_scope() as session:
        applied = current_version(session)

    for version, description, func in sorted(MIGRATIONS, key=lambda item: item[0]):
        if version <= applied:
            continue
        log.info("applying migration %s: %s", version, description)
        with session_scope() as session:
            func(session)
            session.execute(
                text(
                    "INSERT INTO schema_migrations (version, description, applied_at)"
                    " VALUES (:v, :d, :t)"
                ),
                {
                    "v": version,
                    "d": description,
                    "t": datetime.now(UTC).isoformat(),
                },
            )
        applied = version

    # Cheap once-per-startup housekeeping: refresh the query planner's stats.
    with engine.connect() as conn, contextlib.suppress(Exception):
        # Advisory only: the planner refreshes its own statistics.
        conn.execute(text("PRAGMA optimize"))
    return applied


def healthcheck() -> bool:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(select(1))
        return True
    except Exception as exc:  # pragma: no cover - surfaced to /health
        log.error("database healthcheck failed: %s", exc)
        return False
