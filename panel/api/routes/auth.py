"""Authentication and account management."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import qrcode
from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from ...config import get_settings
from ...core import audit
from ...core.crypto import token_fingerprint
from ...core.logging import get_logger
from ...core.security import (
    TokenError,
    create_session,
    decode_token,
    generate_password,
    hash_password,
    needs_rehash,
    new_totp_secret,
    password_strength_ok,
    totp_provisioning_uri,
    verify_password,
    verify_totp,
)
from ...models import ApiToken, RefreshToken, Role, User, utcnow
from ..deps import (
    AdminOnly,
    Context,
    DbSession,
    OptionalPrincipal,
    PrincipalDep,
    enforce_origin,
    failures,
    login_limiter,
)
from ..schemas import (
    AccountOut,
    ApiTokenCreated,
    ApiTokenCreateRequest,
    ApiTokenOut,
    LoginRequest,
    PasswordChangeRequest,
    SessionOut,
    TokenResponse,
    TotpDisableRequest,
    TotpEnableRequest,
    TotpSetupResponse,
    UserCreateRequest,
    UserUpdateRequest,
)

log = get_logger("api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "panah_refresh"


def _set_refresh_cookie(response: Response, token: str, expires: datetime, settings) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api/auth",
        expires=expires,
        max_age=int((expires - utcnow()).total_seconds()),
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE, path="/api/auth")


def _issue(session, user: User, response: Response, ctx) -> TokenResponse:
    settings = get_settings()
    tokens = create_session(
        secret=settings.secret_key,
        subject=user.username,
        user_id=user.id,
        role=user.role,
        access_minutes=settings.access_token_minutes,
        refresh_days=settings.refresh_token_days,
    )
    session.add(
        RefreshToken(
            user_id=user.id,
            jti=tokens.refresh_jti,
            fingerprint=token_fingerprint(tokens.refresh_token),
            expires_at=tokens.refresh_expires_at,
            ip=ctx.ip,
            user_agent=(ctx.user_agent or "")[:255] or None,
        )
    )
    _set_refresh_cookie(response, tokens.refresh_token, tokens.refresh_expires_at, settings)
    return TokenResponse(
        access_token=tokens.access_token,
        expires_at=tokens.access_expires_at,
        username=user.username,
        role=user.role,
        must_change_password=user.must_change_password,
        totp_enabled=user.totp_enabled,
    )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    response: Response,
    session: DbSession,
    ctx: Context,
    request: Request,
) -> TokenResponse:
    enforce_origin(request)
    identity = f"{ctx.ip}:{payload.username.lower()}"

    locked_for = failures.is_locked(identity)
    if locked_for > 0:
        audit.record(
            session,
            action="auth.lockout",
            username=payload.username,
            level="warning",
            success=False,
            ip=ctx.ip,
            user_agent=ctx.user_agent,
            detail={"reason": f"locked for {int(locked_for)}s"},
        )
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"too many attempts; try again in {int(locked_for)} seconds",
        )

    allowed, retry_after = login_limiter.check(ctx.ip)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    user = session.scalar(select(User).where(User.username == payload.username))
    # Always run a hash comparison so a missing user and a wrong password take
    # the same amount of time.
    reference_hash = user.password_hash if user else hash_password("reference-value")
    password_ok = verify_password(payload.password, reference_hash)

    if user is None or not password_ok:
        count = failures.record_failure(identity)
        audit.record(
            session,
            action="auth.login",
            username=payload.username,
            level="warning",
            success=False,
            ip=ctx.ip,
            user_agent=ctx.user_agent,
            detail={"reason": "invalid credentials", "attempt": count},
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password"
        )

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is disabled")
    if user.is_locked():
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="account temporarily locked")

    if user.totp_enabled:
        secret = user.totp_secret or ""
        if not payload.totp:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="two-factor code required"
            )
        if not verify_totp(secret, payload.totp):
            failures.record_failure(identity)
            audit.record(
                session,
                action="auth.login",
                username=user.username,
                level="warning",
                success=False,
                ip=ctx.ip,
                detail={"reason": "invalid two-factor code"},
            )
            session.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid two-factor code"
            )

    failures.record_success(identity)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    user.last_login_at = utcnow()
    user.last_login_ip = ctx.ip
    user.failed_attempts = 0

    result = _issue(session, user, response, ctx)
    audit.record(
        session,
        action="auth.login",
        username=user.username,
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        detail={"role": user.role},
    )
    return result


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    response: Response,
    session: DbSession,
    ctx: Context,
    request: Request,
) -> TokenResponse:
    enforce_origin(request)
    settings = get_settings()
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="no session cookie")

    try:
        payload = decode_token(token, secret=settings.secret_key, expected_type="refresh")
    except TokenError as exc:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    jti = str(payload.get("jti", ""))
    record = session.scalar(select(RefreshToken).where(RefreshToken.jti == jti))
    fingerprint = token_fingerprint(token)

    if record is None or record.revoked or record.fingerprint != fingerprint:
        # Either an unknown token or a replayed one: drop every session for the
        # affected user, because the cookie was likely stolen.
        if record is not None:
            log.warning("refresh token reuse detected for user %s", record.user_id)
            for other in session.scalars(
                select(RefreshToken).where(RefreshToken.user_id == record.user_id)
            ):
                other.revoked = True
            audit.record(
                session,
                action="auth.token_reuse",
                username=payload.get("sub"),
                level="error",
                success=False,
                ip=ctx.ip,
                detail={"reason": "refresh token replay"},
            )
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session")

    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= utcnow():
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired")

    user = session.get(User, record.user_id)
    if user is None or not user.is_active:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account disabled")

    # Rotate: the presented refresh token is retired as soon as it is used.
    record.revoked = True
    return _issue(session, user, response, ctx)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    session: DbSession,
    principal: OptionalPrincipal,
    request: Request,
) -> Response:
    """Sign out.

    Deliberately tolerant: the refresh cookie is revoked and cleared even when
    the access token has already expired or is missing, so an operator can
    always leave the panel.
    """
    username = principal.username if principal is not None else ""
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            payload = decode_token(
                token, secret=get_settings().secret_key, expected_type="refresh"
            )
            record = session.scalar(
                select(RefreshToken).where(RefreshToken.jti == str(payload.get("jti", "")))
            )
            if record:
                record.revoked = True
            if not username:
                subject = session.get(User, int(payload.get("uid", 0) or 0))
                username = subject.username if subject is not None else ""
        except (TokenError, TypeError, ValueError):
            pass
    _clear_refresh_cookie(response)
    audit.record(session, action="auth.logout", username=username or "unknown")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=AccountOut)
def me(session: DbSession, principal: PrincipalDep) -> User:
    if principal.user is None:
        # Machine token: present a synthetic view.
        user = User(
            id=0,
            username=principal.username,
            role=principal.role,
            is_active=True,
            password_hash="",
        )
        return user
    return principal.user


# ---------------------------------------------------------------------------
# Password and second factor
# ---------------------------------------------------------------------------


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: PasswordChangeRequest,
    session: DbSession,
    principal: PrincipalDep,
    ctx: Context,
) -> Response:
    user = principal.user
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not available for tokens")
    if not verify_password(payload.current_password, user.password_hash):
        audit.record(
            session,
            action="auth.password",
            username=user.username,
            success=False,
            level="warning",
            ip=ctx.ip,
            detail={"reason": "current password incorrect"},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="current password is incorrect"
        )
    ok, reason = password_strength_ok(payload.new_password)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    for token in session.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id)):
        token.revoked = True
    audit.record(session, action="auth.password", username=user.username, ip=ctx.ip)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/totp/setup", response_model=TotpSetupResponse)
def totp_setup(session: DbSession, principal: PrincipalDep) -> TotpSetupResponse:
    user = principal.user
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not available for tokens")
    secret = new_totp_secret()
    uri = totp_provisioning_uri(secret, user.username)
    image = qrcode.make(uri)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    import base64

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    # The secret is only stored once the operator proves possession of it.
    audit.record(session, action="auth.totp_setup", username=user.username)
    return TotpSetupResponse(
        secret=secret, provisioning_uri=uri, qr_svg=f"data:image/png;base64,{encoded}"
    )


@router.post("/totp/enable", status_code=status.HTTP_204_NO_CONTENT)
def totp_enable(
    payload: TotpEnableRequest,
    session: DbSession,
    principal: PrincipalDep,
    ctx: Context,
) -> Response:
    user = principal.user
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not available for tokens")
    if not verify_totp(payload.secret, payload.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="the code did not match"
        )
    user.totp_secret = payload.secret
    user.totp_enabled = True
    audit.record(session, action="auth.totp_enabled", username=user.username, ip=ctx.ip)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/totp/disable", status_code=status.HTTP_204_NO_CONTENT)
def totp_disable(
    payload: TotpDisableRequest,
    session: DbSession,
    principal: PrincipalDep,
    ctx: Context,
) -> Response:
    user = principal.user
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not available for tokens")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password is incorrect")
    if user.totp_enabled and payload.code and not verify_totp(user.totp_secret or "", payload.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="two-factor code is incorrect"
        )
    user.totp_enabled = False
    user.totp_secret = None
    audit.record(session, action="auth.totp_disabled", username=user.username, ip=ctx.ip)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(session: DbSession, principal: PrincipalDep) -> list[SessionOut]:
    user = principal.user
    if user is None:
        return []
    rows = session.scalars(
        select(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
        .order_by(RefreshToken.created_at.desc())
        .limit(50)
    ).all()
    return [
        SessionOut(
            id=row.id,
            jti=row.jti,
            ip=row.ip,
            user_agent=row.user_agent,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
        for row in rows
    ]


@router.delete("/sessions", status_code=status.HTTP_204_NO_CONTENT)
def revoke_sessions(session: DbSession, principal: PrincipalDep) -> Response:
    user = principal.user
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not available for tokens")
    for token in session.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id)):
        token.revoked = True
    audit.record(session, action="auth.sessions_revoked", username=user.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Users (admin)
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[AccountOut])
def list_users(session: DbSession, principal: AdminOnly) -> list[User]:
    return list(session.scalars(select(User).order_by(User.id)))


@router.post("/users", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
) -> User:
    if session.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username already exists")
    password = payload.password or generate_password()
    user = User(
        username=payload.username,
        password_hash=hash_password(password),
        email=payload.email,
        role=payload.role,
        is_active=True,
        must_change_password=payload.password is None,
    )
    session.add(user)
    session.flush()
    audit.record(
        session,
        action="user.create",
        username=principal.username,
        target_type="user",
        target_id=user.id,
        ip=ctx.ip,
        detail={"role": payload.role, "generated_password": payload.password is None},
    )
    return user


@router.patch("/users/{user_id}", response_model=AccountOut)
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    if principal.user is not None and user.id == principal.user.id:
        if payload.role and payload.role != Role.ADMIN.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="you cannot remove your own administrator role",
            )
        if payload.is_active is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="you cannot deactivate your own account",
            )

    if payload.email is not None:
        user.email = payload.email
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password:
        ok, reason = password_strength_ok(payload.password)
        if not ok:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)
        user.password_hash = hash_password(payload.password)
        user.must_change_password = True
        for token in session.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id)):
            token.revoked = True

    audit.record(
        session,
        action="user.update",
        username=principal.username,
        target_type="user",
        target_id=user.id,
        ip=ctx.ip,
    )
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
) -> Response:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    if principal.user and user.id == principal.user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="you cannot delete your own account"
        )
    remaining_admins = session.scalars(
        select(User).where(User.role == Role.ADMIN.value, User.id != user.id)
    ).all()
    if user.role == Role.ADMIN.value and not remaining_admins:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="at least one administrator is required"
        )
    session.delete(user)
    audit.record(
        session,
        action="user.delete",
        username=principal.username,
        target_type="user",
        target_id=user_id,
        level="warning",
        ip=ctx.ip,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Machine tokens
# ---------------------------------------------------------------------------


@router.get("/tokens", response_model=list[ApiTokenOut])
def list_tokens(session: DbSession, principal: AdminOnly) -> list[ApiToken]:
    return list(session.scalars(select(ApiToken).order_by(ApiToken.id.desc())))


@router.post("/tokens", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
def create_token(
    payload: ApiTokenCreateRequest,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
) -> ApiTokenCreated:
    import secrets

    raw = f"panah_{secrets.token_urlsafe(32)}"
    expires_at = (
        utcnow() + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )
    record = ApiToken(
        name=payload.name,
        fingerprint=token_fingerprint(raw),
        role=payload.role,
        scopes=payload.scopes,
        created_by=principal.username,
        expires_at=expires_at,
    )
    session.add(record)
    session.flush()
    audit.record(
        session,
        action="token.create",
        username=principal.username,
        target_type="api_token",
        target_id=record.id,
        ip=ctx.ip,
        detail={"role": payload.role},
    )
    return ApiTokenCreated(
        **ApiTokenOut.model_validate(record).model_dump(),
        token=raw,
    )


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_token(
    token_id: int,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
) -> Response:
    record = session.get(ApiToken, token_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    session.delete(record)
    audit.record(
        session,
        action="token.delete",
        username=principal.username,
        target_type="api_token",
        target_id=token_id,
        level="warning",
        ip=ctx.ip,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
