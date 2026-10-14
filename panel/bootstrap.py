"""First-run provisioning.

Creates the schema, the bootstrap administrator and the local node row. Safe to
run repeatedly: it never overwrites an existing account and it never rotates a
password that an operator has already set.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from .config import Settings, get_settings
from .core.logging import get_logger
from .core.security import generate_password, hash_password
from .db import init_engine, run_migrations, session_scope
from .models import Node, NodeStatus, Role, User

log = get_logger("bootstrap")


@dataclass(slots=True)
class BootstrapResult:
    schema_version: int
    admin_username: str | None = None
    admin_password: str | None = None
    admin_created: bool = False
    password_generated: bool = False
    local_node_created: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "admin_username": self.admin_username,
            "admin_created": self.admin_created,
            "password_generated": self.password_generated,
            "local_node_created": self.local_node_created,
        }


def bootstrap(settings: Settings | None = None) -> BootstrapResult:
    settings = settings or get_settings()
    engine = init_engine(settings.database_path)
    version = run_migrations(engine)

    result = BootstrapResult(schema_version=version)

    with session_scope() as session:
        admin = session.scalar(select(User).order_by(User.id))
        if admin is None:
            username = settings.admin_username or "admin"
            password = settings.admin_password
            generated = password is None
            if generated:
                password = generate_password()

            user = User(
                username=username,
                password_hash=hash_password(password),
                role=Role.ADMIN.value,
                is_active=True,
                must_change_password=generated,
            )
            session.add(user)
            result.admin_username = username
            result.admin_password = password
            result.admin_created = True
            result.password_generated = generated
            log.info("created bootstrap administrator %s", username)
        else:
            result.admin_username = admin.username

        local = session.scalar(select(Node).where(Node.is_local.is_(True)))
        if local is None:
            session.add(
                Node(
                    name="local",
                    address="127.0.0.1",
                    agent_port=settings.agent_port,
                    is_local=True,
                    enabled=True,
                    scheme="http",
                    status=NodeStatus.UNKNOWN.value,
                    note="runs on the panel host",
                )
            )
            result.local_node_created = True
            log.info("registered the local node")

    return result
