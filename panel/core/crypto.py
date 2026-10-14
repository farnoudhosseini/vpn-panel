"""Symmetric encryption for values that must be recoverable.

Node API keys and tunnel tokens are needed verbatim when the panel talks to a
remote host, so hashing alone is not enough. Those fields are encrypted with
Fernet (AES-128-CBC + HMAC) using a key stored outside the repository.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .logging import get_logger
from .secrets import load_secret

log = get_logger("crypto")

# Prefix so encrypted values are recognisable and idempotent.
_PREFIX = "enc:v1:"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = load_secret("encryption_key")
    if not key:
        raise RuntimeError("PANAH_ENCRYPTION_KEY is not configured")
    key_bytes = key.encode("ascii")
    try:
        return Fernet(key_bytes)
    except (ValueError, TypeError):
        # Accept arbitrary passphrases and derive a valid 32-byte key.
        digest = hashlib.sha256(key_bytes).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext``; empty input is returned unchanged."""
    if not plaintext:
        return plaintext
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt(value: str) -> str:
    """Decrypt a value produced by :func:`encrypt`.

    Plain values that were never encrypted (for example data written by a
    previous release) are returned as-is so upgrades stay backward compatible.
    """
    if not value:
        return value
    if not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        log.error("failed to decrypt a stored secret; the encryption key changed")
        raise


def token_fingerprint(token: str) -> str:
    """Stable, non-reversible identifier for a token (safe to log and index)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
