"""
JWT auth utilities.

Design notes:
  - Tokens carry `sub` (user_ref) and `role` (customer | analyst | admin).
    `role` drives authorization decisions in `auth_middleware.py` — a
    "customer" token can only act on its own user_ref; "analyst"/"admin"
    tokens can act across users (needed for fraud-review workflows).
  - Real identity verification (OTP via SMS gateway, or a proper IdP like
    AWS Cognito) is NOT implemented here — see `verify_login_otp()` below,
    which is a clearly-marked stub. Wire it to your SMS OTP provider (or
    Cognito/Auth0) before this touches production traffic. Never ship the
    dev fallback OTP to a real deployment.
"""
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings

settings = get_settings()

VALID_ROLES = {"customer", "analyst", "admin"}


def create_access_token(user_ref: str, role: str = "customer", expiry_minutes: int | None = None) -> str:
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of {VALID_ROLES}")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_ref,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=expiry_minutes or settings.jwt_expiry_minutes),
        "jti": os.urandom(12).hex(),  # unique token id, needed for revocation-list support
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_ref: str, role: str = "customer") -> str:
    """Longer-lived token used only to mint new access tokens via /auth/refresh."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_ref,
        "role": role,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_refresh_expiry_days),
        "jti": os.urandom(12).hex(),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    if payload.get("role") not in VALID_ROLES:
        return None
    # Fix: a refresh token (create_refresh_token sets "type": "refresh") has
    # the same "sub"/"role" shape as an access token and, without this check,
    # was accepted here as a valid Bearer access token — letting a 14-day-lived
    # refresh token do everything a 60-minute access token can do if it leaked.
    # Access tokens never carry a "type" claim, so reject anything that does.
    if payload.get("type") is not None:
        return None
    return payload


def verify_refresh_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "refresh":
        return None
    return payload


def verify_login_otp(user_ref: str, otp: str) -> bool:
    """
    STUB - replace with a real SMS OTP provider (e.g. AWS SNS OTP / MSG91 /
    Twilio Verify) or an IdP-based login (Cognito) before production use.

    Dev/local behaviour only: accepts a fixed OTP defined by
    DEV_FALLBACK_OTP (default "000000") when environment == "local".
    In staging/production this always returns False until a real
    provider is wired in, so the auth endpoint fails closed rather than
    silently accepting a hardcoded code.
    """
    if settings.environment != "local":
        return False
    return hmac.compare_digest(otp, settings.dev_fallback_otp)


def is_secret_strong_enough(secret: str, min_length: int = 32) -> bool:
    """Rejects empty/default/short secrets. Called at startup for
    staging/production - see `app/config.py::validate_production_config`."""
    if not secret:
        return False
    if secret in {"****************************", "secret", "changeme"}:
        return False
    return len(secret) >= min_length
