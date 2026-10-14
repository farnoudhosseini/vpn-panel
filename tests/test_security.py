"""Passwords, tokens, two-factor and subscription signatures."""

from __future__ import annotations

from datetime import timedelta

import pytest

from panel.core import security


def test_password_round_trip() -> None:
    stored = security.hash_password("Correct-Horse-Battery-1!")
    assert stored != "Correct-Horse-Battery-1!"
    assert security.verify_password("Correct-Horse-Battery-1!", stored)
    assert not security.verify_password("wrong", stored)


def test_password_hash_is_salted() -> None:
    assert security.hash_password("same-password-123") != security.hash_password(
        "same-password-123"
    )


def test_verify_rejects_malformed_hash() -> None:
    assert security.verify_password("x", "not-a-hash") is False


@pytest.mark.parametrize(
    ("password", "expected"),
    [
        ("short", False),
        ("alllowercaseletters", False),
        ("Alllowercaseletters1", True),
        ("Str0ng-Enough-Pass!", True),
    ],
)
def test_password_strength(password: str, expected: bool) -> None:
    ok, _ = security.password_strength_ok(password)
    assert ok is expected


def test_generated_passwords_are_strong() -> None:
    password = security.generate_password()
    ok, reason = security.password_strength_ok(password)
    assert ok, reason
    assert len(password) >= 16


def make_tokens(settings, *, access_minutes: int = 30, refresh_days: int = 7):
    return security.create_session(
        secret=settings.secret_key,
        subject="1",
        user_id=1,
        role="admin",
        access_minutes=access_minutes,
        refresh_days=refresh_days,
    )


def test_access_token_round_trip(settings) -> None:
    tokens = make_tokens(settings)
    claims = security.decode_token(
        tokens.access_token, secret=settings.secret_key, expected_type="access"
    )
    assert claims["sub"] == "1"
    assert claims["uid"] == 1
    assert claims["role"] == "admin"


def test_refresh_token_cannot_be_used_as_access(settings) -> None:
    tokens = make_tokens(settings)
    with pytest.raises(security.TokenError):
        security.decode_token(
            tokens.refresh_token, secret=settings.secret_key, expected_type="access"
        )


def test_token_signed_with_another_secret_is_rejected(settings) -> None:
    tokens = make_tokens(settings)
    with pytest.raises(security.TokenError):
        security.decode_token(
            tokens.access_token, secret="a-different-secret-value", expected_type="access"
        )


def test_expired_token_is_rejected(settings) -> None:
    token, _, _ = security.create_token(
        secret=settings.secret_key,
        subject="1",
        user_id=1,
        role="admin",
        token_type="access",
        expires_delta=timedelta(seconds=-5),
    )
    with pytest.raises(security.TokenError):
        security.decode_token(token, secret=settings.secret_key, expected_type="access")


def test_totp_verification_accepts_the_current_code() -> None:
    import pyotp

    secret = security.new_totp_secret()
    code = pyotp.TOTP(secret).now()
    assert security.verify_totp(secret, code)
    assert not security.verify_totp(secret, "000000")


def test_totp_provisioning_uri() -> None:
    secret = security.new_totp_secret()
    uri = security.totp_provisioning_uri(secret, "admin")
    assert uri.startswith("otpauth://totp/")
    assert "secret=" in uri


def test_subscription_signature() -> None:
    signed = security.sign_subscription("sub-123", "secret-value")
    assert security.verify_subscription_signature("sub-123", signed, "secret-value")
    assert not security.verify_subscription_signature("sub-124", signed, "secret-value")
    assert not security.verify_subscription_signature("sub-123", signed, "other-secret")


def test_common_name_and_sub_id_are_filesystem_safe() -> None:
    for _ in range(20):
        common_name = security.generate_common_name()
        sub_id = security.generate_sub_id()
        # They become file names (ccd entries, bundles) and URL path segments.
        assert common_name.isascii()
        assert "/" not in common_name and ".." not in common_name
        assert "/" not in sub_id and " " not in sub_id


def test_constant_time_equals() -> None:
    assert security.constant_time_equals("abc", "abc")
    assert not security.constant_time_equals("abc", "abd")
    assert not security.constant_time_equals("", "abc")
