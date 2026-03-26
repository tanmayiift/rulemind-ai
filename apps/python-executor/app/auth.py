from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
import jwt


JWT_COOKIE_NAME = "rulemind_admin_session"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bcrypt_hash(value: str) -> str:
    return bcrypt.hashpw(value.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def bcrypt_verify(value: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(value.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def generate_api_key() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "rm_live_" + "".join(secrets.choice(alphabet) for _ in range(32))


def mask_api_key(key: str) -> str:
    if len(key) <= 12:
        return key[:4] + "****"
    return key[:8] + "****" + key[-3:]


def key_lookup_hash(key: str) -> str:
    return sha256_hex(key)


def api_key_kid(key: str) -> str:
    return "key_" + sha256_hex(key)[:12]


def create_admin_jwt(subject: str, email: str, expires_hours: int = 12) -> str:
    secret = os.getenv("RULEMIND_ADMIN_JWT_SECRET", "rulemind-admin-dev-secret")
    payload = {
        "sub": subject,
        "email": email,
        "exp": utcnow() + timedelta(hours=expires_hours),
        "iat": utcnow(),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_admin_jwt(token: str) -> Dict[str, Any]:
    secret = os.getenv("RULEMIND_ADMIN_JWT_SECRET", "rulemind-admin-dev-secret")
    return jwt.decode(token, secret, algorithms=["HS256"])


def generate_rsa_keypair_material() -> Dict[str, str]:
    # Placeholder-friendly development helper. The actual compiler path uses a symmetric
    # transport fallback when client public key encryption is unavailable.
    seed = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8")
    return {"public": seed, "private": seed}


def hmac_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_hmac_signature(secret: str, body: bytes, signature: Optional[str]) -> bool:
    if not signature:
        return False
    expected = hmac_signature(secret, body)
    return hmac.compare_digest(expected, signature)
