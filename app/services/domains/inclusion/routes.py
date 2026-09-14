"""
Inclusion domain routes — BlackSheep Router.
Mounted at /api/v1/inclusion in app/main.py.

Auth: requires a valid Bearer token (enforced by app.core.auth_middleware).
Authorization: a "customer" token may only score/read its own user_ref;
"analyst"/"admin" tokens may act on any user_ref (needed for support/ops
review of a customer's score).
"""
from blacksheep import Request, json
from blacksheep.server.controllers import APIController, get, post
from pydantic import ValidationError
from sqlalchemy import select

from app.core.audit import log_audit_event
from app.core.auth_middleware import ensure_owner_or_role, require_identity
from app.db.postgres import AsyncSessionLocal
from app.db.redis_client import check_rate_limit
from app.models.postgres_models import User
from app.services.domains.inclusion.schemas import CreditScoreRequest
from app.services.domains.inclusion.service import InclusionService

_ANALYST_ROLES = ("analyst", "admin")


class InclusionController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/inclusion"

    @post("/credit-score")
    async def compute_credit_score(self, request: Request) -> json:
        """Body: { "user_ref": "USR-1001", "alt_data": {...}, "llm_provider": "anthropic" (optional) }"""
        identity = require_identity(request)

        raw = await request.json()
        try:
            body = CreditScoreRequest.model_validate(raw)
        except ValidationError as e:
            return json({"error": "Invalid request", "details": e.errors()}, status=400)

        deny = ensure_owner_or_role(identity, body.user_ref, allowed_roles=_ANALYST_ROLES)
        if deny:
            return deny

        allowed, _ = await check_rate_limit(identity["sub"], route="inclusion_credit_score", limit=15, window_seconds=60)
        if not allowed:
            return json({"error": "Rate limit exceeded. Try again shortly."}, status=429)

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == body.user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            service = InclusionService(db)
            result = await service.score_user(user, body.alt_data.model_dump(), provider_override=body.llm_provider)

        await log_audit_event(
            action="credit_score.compute", actor_ref=identity["sub"], actor_role=identity.get("role"),
            target_user_ref=body.user_ref, details={"score": result.get("score"), "band": result.get("band")},
        )
        return json(result)

    @get("/users/{user_ref}/scores")
    async def get_score_history(self, request: Request, user_ref: str) -> json:
        from app.models.postgres_models import CreditScore

        identity = require_identity(request)
        deny = ensure_owner_or_role(identity, user_ref, allowed_roles=_ANALYST_ROLES)
        if deny:
            return deny

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            scores_result = await db.execute(
                select(CreditScore).where(CreditScore.user_id == user.id).order_by(CreditScore.computed_at.desc())
            )
            scores = scores_result.scalars().all()
            return json(
                {
                    "user_ref": user_ref,
                    "scores": [
                        {"score": s.score, "band": s.score_band, "computed_at": s.computed_at.isoformat(), "explanation": s.explanation}
                        for s in scores
                    ],
                }
            )
