"""
Inclusion domain routes — BlackSheep Router.
Mounted at /api/v1/inclusion in app/main.py.
"""
from blacksheep import Request, json
from blacksheep.server.controllers import APIController, get, post
from sqlalchemy import select

from app.db.postgres import AsyncSessionLocal
from app.models.postgres_models import User
from app.services.domains.inclusion.service import InclusionService


class InclusionController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/inclusion"

    @post("/credit-score")
    async def compute_credit_score(self, request: Request) -> json:
        """Body: { "user_ref": "USR-1001", "alt_data": {...}, "llm_provider": "anthropic" (optional) }"""
        payload = await request.json()
        user_ref = payload.get("user_ref")
        alt_data = payload.get("alt_data")
        if not user_ref or not alt_data:
            return json({"error": "user_ref and alt_data are required"}, status=400)

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            service = InclusionService(db)
            result = await service.score_user(user, alt_data, provider_override=payload.get("llm_provider"))
            return json(result)

    @get("/users/{user_ref}/scores")
    async def get_score_history(self, user_ref: str) -> json:
        from app.models.postgres_models import CreditScore

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

    @post("/users/{user_ref}/coach")
    async def coach(self, request: Request, user_ref: str) -> json:
        """Credit-improvement coach: extends the one-off score explanation
        into actionable coaching, using the signal breakdown stored with the
        user's most recent score (or one passed in the request body under
        'signal_breakdown', for a what-if scenario).
        Body (optional): { "signal_breakdown": {...} }"""
        from app.services.domains.inclusion.agent import InclusionCoachAgent
        from app.models.postgres_models import CreditScore

        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            latest_result = await db.execute(
                select(CreditScore).where(CreditScore.user_id == user.id).order_by(CreditScore.computed_at.desc()).limit(1)
            )
            latest = latest_result.scalar_one_or_none()

            signal_breakdown = payload.get("signal_breakdown") or (latest.signal_breakdown if latest else None)
            if signal_breakdown is None:
                return json(
                    {"error": "No credit score on file for this user yet — compute one first via POST /credit-score"},
                    status=400,
                )

            score = latest.score if latest else 0
            band = latest.score_band if latest else "unknown"

            agent = InclusionCoachAgent()
            advice = await agent.coach(score, band, signal_breakdown)
            return json({"user_ref": user_ref, "coaching": advice})
