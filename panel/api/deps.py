"""Shared API dependencies.

Access tokens travel in the ``Authorization`` header and are never stored in a
cookie the browser sends automatically, which removes the classic CSRF class of
attack. The only cookie is the refresh token, and that endpoint additionally
verifies the request origin.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from ..config import get_settings
from ..core.crypto import token_fingerprint
from ..core.logging import get_logger
from ..core.ratelimit import FailureTracker, RateLimiter, ip_in_allowlist, normalise_ip
from ..core.security import TokenError, decode_token
from ..db import get_db
from ..models import ApiToken, Role, User, utcnow
from ..services import scheduler

log = get_logger("api.deps")

ROLE_ORDER = {Role.VIEWER.value: 1, Role.OPERATOR.value: 2, Role.ADMIN.value: 3}

request_limiter = RateLimiter(limit=get_settings().rate_limit_per_minute, window_seconds=60)
login_limiter = RateLimiter(limit=10, window_seconds=60)
failures = FailureTracker(
    threshold=get_settings().max_login_attempts,
    lock_seconds=get_settings().login_lock_seconds,
)

DbSession = Annotated[OrmSession, Depends(get_db)]


@dataclass(slots=True)
class RequestContext:
    ip: str
    user_agent: str | None
    origin: str | None
    request_id: str


@dataclass(slots=True)
class Principal:
    """Who is making the request."""

    username: str
    role: str
    user: User | None = None
    token_name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value

    def at_least(self, role: str) -> bool:
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER.get(role, 99)


def client_ip(request: Request) -> str:
    settings = get_settings()
    trusted = set(settings.trusted_proxy_list)
    peer = normalise_ip(request.client.host if request.client else None)
    if peer in trusted:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return normalise_ip(forwarded.split(",")[0])
        real = request.headers.get("x-real-ip")
        if real:
            return normalise_ip(real)
    return peer


def context(request: Request) -> RequestContext:
    return RequestContext(
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        origin=request.headers.get("origin"),
        request_id=getattr(request.state, "request_id", ""),
    )


Context = Annotated[RequestContext, Depends(context)]


def enforce_allowlist(request: Request) -> None:
    settings = get_settings()
    entries = settings.ip_allowlist_entries
    if not entries:
        return
    if not ip_in_allowlist(client_ip(request), entries):
        log.warning("blocked request from %s: not in IP allowlist", client_ip(request))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not allowed")


def enforce_origin(request: Request) -> None:
    """Reject cross-site requests for cookie-authenticated endpoints."""
    settings = get_settings()
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        # Non-browser clients (curl, the CLI) are fine; they cannot be tricked
        # into sending an ambient cookie.
        return
    allowed = settings.allowed_origin_list_fallback
    normalised = origin.rstrip("/")
    if not any(normalised == candidate.rstrip("/") for candidate in allowed):
        log.warning("rejected cross-origin request from %s", normalised)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origin not allowed")


def _api_token_principal(session: OrmSession, token: str) -> Principal | None:
    fingerprint = token_fingerprint(token)
    record = session.scalar(
        select(ApiToken).where(ApiToken.fingerprint == fingerprint, ApiToken.is_active.is_(True))
    )
    if record is None:
        return None
    if record.expires_at:
        expires = record.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= utcnow():
            return None
    record.last_used_at = utcnow()
    return Principal(username=f"token:{record.name}", role=record.role, token_name=record.name)


def current_principal(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")

    settings = get_settings()
    try:
        payload = decode_token(token, secret=settings.secret_key, expected_type="access")
    except TokenError as exc:
        # Machine tokens are not JWTs; check those before giving up.
        principal = _api_token_principal(session, token)
        if principal is not None:
            return principal
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user_id = payload.get("uid")
    user = session.get(User, int(user_id)) if user_id is not None else None
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account disabled")
    if user.is_locked():
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="account temporarily locked")

    return Principal(username=user.username, role=user.role, user=user)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def optional_principal(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """Authenticate when a credential is present, but never reject.

    Used by sign-out: an expired access token must not trap a signed-in
    operator inside the panel, and clearing a session is idempotent anyway.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        return current_principal(session=session, authorization=authorization)
    except HTTPException:
        return None


OptionalPrincipal = Annotated[Principal | None, Depends(optional_principal)]


def require_role(*roles: str) -> Callable[[Principal], Principal]:
    allowed = set(roles)

    def dependency(principal: PrincipalDep) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient privileges",
            )
        return principal

    return dependency


def require_min_role(role: str) -> Callable[[Principal], Principal]:
    def dependency(principal: PrincipalDep) -> Principal:
        if not principal.at_least(role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires {role} privileges",
            )
        return principal

    return dependency


AdminOnly = Annotated[Principal, Depends(require_role(Role.ADMIN.value))]
OperatorUp = Annotated[Principal, Depends(require_min_role(Role.OPERATOR.value))]
ViewerUp = Annotated[Principal, Depends(require_min_role(Role.VIEWER.value))]


def rate_limit(key: str, *, limiter: RateLimiter | None = None) -> None:
    active = limiter or request_limiter
    allowed, retry_after = active.check(key)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many requests",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def audit_identity(principal: Principal) -> str:
    return principal.username


def scheduler_state() -> dict[str, object]:
    return {"running": scheduler.is_running(), "jobs": scheduler.job_status()}


def scopes_hint(scopes: Iterable[str]) -> list[str]:
    return sorted(set(scopes))
