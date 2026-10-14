"""Authentication primitives: passwords, sessions and second factors.

Access tokens are short-lived JWTs returned in the response body and held in
memory by the browser, so they are immune to CSRF. Refresh tokens live in an
HttpOnly, SameSite=Strict cookie scoped to the auth routes and are tracked in
the database so a stolen session can be revoked.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# Argon2id defaults tuned for an interactive login on a modest VPS.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

TokenType = Literal["access", "refresh"]
ALGORITHM = "HS256"
ISSUER = "panah"

_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
_CN_ALPHABET = string.ascii_lowercase + string.digits


class TokenError(Exception):
    """Raised when a JWT is missing, malformed, expired or of the wrong type."""


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:  # pragma: no cover - defensive
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def password_strength_ok(password: str, minimum: int = 10) -> tuple[bool, str]:
    if len(password) < minimum:
        return False, f"password must be at least {minimum} characters"
    classes = sum(
        bool(pattern(password))
        for pattern in (
            lambda p: any(c.islower() for c in p),
            lambda p: any(c.isupper() for c in p),
            lambda p: any(c.isdigit() for c in p),
            lambda p: any(c in string.punctuation for c in p),
        )
    )
    if classes < 3:
        return False, "password must mix upper case, lower case, digits and symbols"
    return True, ""


def generate_password(length: int = 20) -> str:
    """Cryptographically strong password for the bootstrap admin."""
    while True:
        candidate = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        ok, _ = password_strength_ok(candidate)
        if ok:
            return candidate


def generate_secret(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def generate_common_name(prefix: str = "u", length: int = 10) -> str:
    suffix = "".join(secrets.choice(_CN_ALPHABET) for _ in range(length))
    return f"{prefix}-{suffix}"


def generate_sub_id() -> str:
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24]


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


# ---------------------------------------------------------------------------
# Sessions (JWT)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionTokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    refresh_jti: str


def _encode(payload: dict[str, Any], secret: str) -> str:
    return jwt.encode(payload, secret, algorithm=ALGORITHM, headers={"typ": "JWT"})


def create_token(
    *,
    secret: str,
    subject: str,
    user_id: int,
    role: str,
    token_type: TokenType,
    expires_delta: timedelta,
    jti: str | None = None,
) -> tuple[str, str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + expires_delta
    token_id = jti or secrets.token_urlsafe(16)
    payload = {
        "sub": subject,
        "uid": user_id,
        "role": role,
        "typ": token_type,
        "iss": ISSUER,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": token_id,
    }
    return _encode(payload, secret), token_id, expires_at


def create_session(
    *,
    secret: str,
    subject: str,
    user_id: int,
    role: str,
    access_minutes: int,
    refresh_days: int,
) -> SessionTokens:
    access_token, _, access_expires = create_token(
        secret=secret,
        subject=subject,
        user_id=user_id,
        role=role,
        token_type="access",
        expires_delta=timedelta(minutes=access_minutes),
    )
    refresh_token, refresh_jti, refresh_expires = create_token(
        secret=secret,
        subject=subject,
        user_id=user_id,
        role=role,
        token_type="refresh",
        expires_delta=timedelta(days=refresh_days),
    )
    return SessionTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=access_expires,
        refresh_expires_at=refresh_expires,
        refresh_jti=refresh_jti,
    )


def decode_token(token: str, *, secret: str, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token invalid") from exc

    if payload.get("typ") != expected_type:
        raise TokenError("unexpected token type")
    return payload


# ---------------------------------------------------------------------------
# Two factor authentication
# ---------------------------------------------------------------------------


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, username: str, issuer: str = "Panah") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    if not secret or not code:
        return False
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit():
        return False
    try:
        return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=valid_window))
    except Exception:  # pragma: no cover - malformed secret
        return False


def generate_recovery_codes(count: int = 8) -> list[str]:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(count)]


# ---------------------------------------------------------------------------
# Subscription signing
# ---------------------------------------------------------------------------


def sign_subscription(sub_id: str, secret: str) -> str:
    """Short signature so subscription URLs cannot be enumerated."""
    digest = hmac.new(secret.encode("utf-8"), sub_id.encode("utf-8"), "sha256").digest()
    return base64.urlsafe_b64encode(digest[:9]).decode("ascii").rstrip("=")


def verify_subscription_signature(sub_id: str, signature: str, secret: str) -> bool:
    return constant_time_equals(sign_subscription(sub_id, secret), signature)
