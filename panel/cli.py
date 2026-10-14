"""Operator command line.

Everything the installer and an administrator need without a browser: start a
service, provision an account, reset a password and check that the host can
actually run OpenVPN.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import PRODUCT, __version__
from .config import get_settings, settings_for_repr
from .core.logging import configure_logging, get_logger
from .core.security import (
    generate_password,
    hash_password,
    new_totp_secret,
    password_strength_ok,
    totp_provisioning_uri,
)
from .db import init_engine, run_migrations, session_scope
from .models import Role, User

log = get_logger("cli")


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    configure_logging(level=settings.log_level, filename="panah.log")
    uvicorn.run(
        "panel.main:get_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=",".join(settings.trusted_proxy_list),
        server_header=False,
        access_log=False,
        ssl_keyfile=args.ssl_keyfile,
        ssl_certfile=args.ssl_certfile,
    )
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    import uvicorn

    from .agent.app import create_agent_app

    settings = get_settings()
    configure_logging(level=settings.log_level, filename="panah-agent.log")
    if not settings.agent_key:
        print("PANAH_AGENT_KEY is not set; refusing to start", file=sys.stderr)
        return 2
    uvicorn.run(
        create_agent_app(),
        host=args.host or "0.0.0.0",
        port=args.port or settings.agent_port,
        proxy_headers=True,
        server_header=False,
        access_log=False,
        ssl_keyfile=args.ssl_keyfile,
        ssl_certfile=args.ssl_certfile,
    )
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from .bootstrap import bootstrap

    configure_logging()
    result = bootstrap()
    _print(result.as_dict())
    if result.admin_created and result.admin_password:
        print("\n=== credentials (shown once) ===")
        print(f"username: {result.admin_username}")
        print(f"password: {result.admin_password}")
        if result.password_generated:
            print("You will be asked to change this password after signing in.")
        print("================================\n")
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    configure_logging()
    init_engine()
    run_migrations()
    password = args.password or generate_password()
    ok, reason = password_strength_ok(password)
    if not ok:
        print(f"password rejected: {reason}", file=sys.stderr)
        return 2

    with session_scope() as session:
        from sqlalchemy import select

        if session.scalar(select(User).where(User.username == args.username)):
            print(f"user {args.username} already exists", file=sys.stderr)
            return 1
        user = User(
            username=args.username,
            password_hash=hash_password(password),
            role=args.role,
            email=args.email,
            is_active=True,
            must_change_password=args.password is None,
        )
        session.add(user)
        session.flush()
        user_id = user.id

    _print({"created": args.username, "id": user_id, "role": args.role})
    if args.password is None:
        print(f"generated password: {password}")
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    configure_logging()
    init_engine()
    run_migrations()
    from sqlalchemy import select

    password = args.password or generate_password()
    ok, reason = password_strength_ok(password)
    if not ok:
        print(f"password rejected: {reason}", file=sys.stderr)
        return 2

    with session_scope() as session:
        user = session.scalar(select(User).where(User.username == args.username))
        if user is None:
            print(f"user {args.username} not found", file=sys.stderr)
            return 1
        user.password_hash = hash_password(password)
        user.failed_attempts = 0
        user.locked_until = None
        user.must_change_password = args.password is None

    _print({"updated": args.username, "generated": args.password is None})
    if args.password is None:
        print(f"new password: {password}")
    return 0


def cmd_totp(args: argparse.Namespace) -> int:
    configure_logging()
    init_engine()
    run_migrations()
    from sqlalchemy import select

    with session_scope() as session:
        user = session.scalar(select(User).where(User.username == args.username))
        if user is None:
            print(f"user {args.username} not found", file=sys.stderr)
            return 1
        secret = new_totp_secret()
        user.totp_secret = secret
        user.totp_enabled = True

    _print(
        {
            "username": args.username,
            "secret": secret,
            "provisioning_uri": totp_provisioning_uri(secret, args.username),
        }
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check that the host can run what the panel manages."""
    configure_logging()
    from .services import certbot, nginx, node_ops, openvpn, plan, shell, tunnels

    settings = get_settings()
    report: dict[str, Any] = {
        "product": PRODUCT,
        "version": __version__,
        "settings": settings_for_repr(),
        "is_root": shell.is_root(),
        "openvpn": openvpn.detect().as_dict(),
        "nginx": nginx.capabilities(),
        "backpack": tunnels.backpack_info() if tunnels.backpack_available() else {"installed": False},
        "certbot": certbot.available(),
        "paths": {
            "config": str(plan.paths.config_dir()),
            "openvpn": str(plan.paths.openvpn_root()),
            "data": str(plan.paths.data_dir()),
        },
    }
    try:
        report["capabilities"] = node_ops.capabilities()
    except Exception as exc:  # pragma: no cover - diagnostic path
        report["capabilities_error"] = str(exc)

    database_ok = True
    try:
        init_engine(settings.database_path)
        run_migrations()
    except Exception as exc:  # pragma: no cover
        database_ok = False
        report["database_error"] = str(exc)
    report["database_ok"] = database_ok

    _print(report)
    problems = []
    if not shell.is_root():
        problems.append("not running as root: OpenVPN and systemd control will fail")
    if not report["openvpn"]["installed"]:
        problems.append("openvpn is not installed")
    if not database_ok:
        problems.append("the database could not be opened")
    return 1 if problems else 0


def cmd_version(_: argparse.Namespace) -> int:
    print(f"{PRODUCT} {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="panah", description=f"{PRODUCT} VPN panel")
    parser.add_argument("--version", action="version", version=f"{PRODUCT} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the panel")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--ssl-keyfile")
    serve.add_argument("--ssl-certfile")
    serve.set_defaults(func=cmd_serve)

    agent = sub.add_parser("agent", help="run the node agent")
    agent.add_argument("--host", default="0.0.0.0")
    agent.add_argument("--port", type=int)
    agent.add_argument("--ssl-keyfile")
    agent.add_argument("--ssl-certfile")
    agent.set_defaults(func=cmd_agent)

    bootstrap = sub.add_parser("bootstrap", help="create schema, admin and local node")
    bootstrap.set_defaults(func=cmd_bootstrap)

    create_user = sub.add_parser("create-user", help="create an account")
    create_user.add_argument("username")
    create_user.add_argument("--password")
    create_user.add_argument("--email")
    create_user.add_argument(
        "--role", default=Role.ADMIN.value, choices=[role.value for role in Role]
    )
    create_user.set_defaults(func=cmd_create_user)

    reset = sub.add_parser("reset-password", help="set a new password")
    reset.add_argument("username")
    reset.add_argument("--password")
    reset.set_defaults(func=cmd_reset_password)

    totp = sub.add_parser("enable-totp", help="enable two-factor authentication")
    totp.add_argument("username")
    totp.set_defaults(func=cmd_totp)

    doctor = sub.add_parser("doctor", help="diagnose the host")
    doctor.set_defaults(func=cmd_doctor)

    version = sub.add_parser("version", help="print the version")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
