"""
Auth domain routes — BlackSheep Router. Mounted at /api/v1/auth.

Deliberately left PUBLIC (see app/core/auth_middleware.PUBLIC_PATH_PREFIXES)
since a caller has no token yet when hitting these. Everything downstream
of a successful call here is what the rest of the API actually enforces.
"""
import hmac

from blacksheep import Request, json
from blacksheep.server.controllers import APIController, post
from pydantic import ValidationError
from sqlalchemy import select

from app.config import get_settings
from app.core.audit import log_audit_event
from app.core.security import (
    create_access_token,
    create_refresh_token,
    verify_login_otp,
    verify_refresh_token,
)
from app.db.postgres import AsyncSessionLocal
from app.db.redis_client import check_rate_limit
from app.models.postgres_models import User
from app.services.domains.auth.schemas import InternalTokenRequest, LoginRequest, RefreshRequest

settings = get_settings()


class AuthController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/auth"

    @post("/login")
    async def login(self, request: Request) -> json:
        """Body: { "user_ref": "USR-1001", "otp": "000000" }
        OTP verification is a stub in local/dev — see
        app/core/security.py::verify_login_otp for what to replace before
        production (real SMS OTP provider or an IdP)."""
        raw = await request.json()

        # Rate-limit login attempts per user_ref to blunt OTP brute-forcing,
        # independent of whether the OTP itself is valid.
        candidate_ref = str(raw.get("user_ref", "unknown"))[:20]
        allowed, _ = await check_rate_limit(candidate_ref, route="auth_login", limit=10, window_seconds=60)
        if not allowed:
            return json({"error": "Too many login attempts. Try again shortly."}, status=429)

        try:
            body = LoginRequest.model_validate(raw)
        except ValidationError as e:
            return json({"error": "Invalid request", "details": e.errors()}, status=400)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_ref == body.user_ref))
            user = result.scalar_one_or_none()

        if user is None or not verify_login_otp(body.user_ref, body.otp):
            await log_audit_event(
                action="auth.token_denied", actor_ref=body.user_ref, actor_role=None,
                target_user_ref=body.user_ref, outcome="failure",
            )
            return json({"error": "Invalid user_ref or OTP"}, status=401)

        access = create_access_token(user.user_ref, role="customer")
        refresh = create_refresh_token(user.user_ref, role="customer")
        await log_audit_event(
            action="auth.token_issue", actor_ref=user.user_ref, actor_role="customer",
            target_user_ref=user.user_ref,
        )
        return json({"access_token": access, "refresh_token": refresh, "token_type": "Bearer"})

    @post("/refresh")
    async def refresh(self, request: Request) -> json:
        """Body: { "refresh_token": "..." } -> issues a new short-lived access token."""
        raw = await request.json()
        try:
            body = RefreshRequest.model_validate(raw)
        except ValidationError as e:
            return json({"error": "Invalid request", "details": e.errors()}, status=400)

        payload = verify_refresh_token(body.refresh_token)
        if payload is None:
            return json({"error": "Invalid or expired refresh token"}, status=401)

        access = create_access_token(payload["sub"], role=payload.get("role", "customer"))
        return json({"access_token": access, "token_type": "Bearer"})

    @post("/internal-token")
    async def internal_token(self, request: Request) -> json:
        """Body: { "admin_bootstrap_key": "...", "user_ref": "ANALYST-1", "role": "analyst" }
        For internal/service accounts (fraud analysts, ops) only. Requires
        INTERNAL_ADMIN_BOOTSTRAP_KEY to be configured — refuses to issue
        anything if it isn't set, so this can never be exploited via an
        unset/default env var."""
        raw = await request.json()
        try:
            body = InternalTokenRequest.model_validate(raw)
        except ValidationError as e:
            return json({"error": "Invalid request", "details": e.errors()}, status=400)

        if not settings.internal_admin_bootstrap_key:
            return json({"error": "Internal token issuance is not configured on this deployment."}, status=403)

        if not hmac.compare_digest(body.admin_bootstrap_key, settings.internal_admin_bootstrap_key):
            await log_audit_event(
                action="auth.internal_token_denied", actor_ref=body.user_ref, actor_role=body.role,
                target_user_ref=body.user_ref, outcome="failure",
            )
            return json({"error": "Invalid bootstrap key"}, status=401)

        access = create_access_token(body.user_ref, role=body.role)
        await log_audit_event(
            action="auth.internal_token_issue", actor_ref=body.user_ref, actor_role=body.role,
            target_user_ref=body.user_ref,
        )
        return json({"access_token": access, "token_type": "Bearer"})
