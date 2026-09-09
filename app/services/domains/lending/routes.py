"""
Lending domain routes — BlackSheep Router.
Mounted at /api/v1/lending in app/main.py.
"""
from blacksheep import Request, json
from blacksheep.server.controllers import APIController, post
from sqlalchemy import select

from app.db.postgres import AsyncSessionLocal
from app.models.postgres_models import CreditScore, User
from app.services.domains.lending.service import LendingService


class LendingController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/lending"

    @post("/applications")
    async def submit_application(self, request: Request) -> json:
        """Body: { "user_ref": "USR-1005", "loan_type": "msme_working_capital",
        "requested_amount_inr": 300000, "purpose": "...", "collateral_provided": false,
        "gst_data_available": true, "bank_statement_months_provided": 12 }"""
        payload = await request.json()
        user_ref = payload.get("user_ref")
        if not user_ref:
            return json({"error": "user_ref is required"}, status=400)

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            latest_score_result = await db.execute(
                select(CreditScore).where(CreditScore.user_id == user.id).order_by(CreditScore.computed_at.desc()).limit(1)
            )
            latest_score = latest_score_result.scalar_one_or_none()

            service = LendingService(db)
            result = await service.submit_application(
                user, payload, latest_credit_score=latest_score.score if latest_score else None
            )
            return json(result)
