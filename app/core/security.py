"""
JWT auth utilities. In production, sit this behind AWS Cognito or your
own identity provider; this module handles service-level token issue/verify.
"""
from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings

settings = get_settings()


def create_access_token(user_ref: str) -> str:
    payload = {
        "sub": user_ref,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expiry_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
