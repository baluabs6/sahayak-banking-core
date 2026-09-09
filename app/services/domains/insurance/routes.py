"""
Insurance domain routes — BlackSheep Router.
Mounted at /api/v1/insurance in app/main.py.
"""
from blacksheep import Request, json
from blacksheep.server.controllers import APIController, post
from sqlalchemy import select

from app.db.postgres import AsyncSessionLocal
from app.models.postgres_models import User
from app.services.domains.insurance.service import InsuranceService


class InsuranceController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/insurance"

    @post("/claims")
    async def file_claim(self, request: Request) -> json:
        """Body: { "user_ref": "USR-1001", "policy_type": "crop_insurance",
        "trigger_event": "rainfall_deficit", "region": "Nashik, MH",
        "claim_amount_inr": 22000, "parametric_data_source": "IMD_rainfall_api",
        "index_value": 42.0, "index_threshold": 30.0 }"""
        payload = await request.json()
        user_ref = payload.get("user_ref")
        if not user_ref:
            return json({"error": "user_ref is required"}, status=400)

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            service = InsuranceService(db)
            result = await service.file_claim(user, payload)
            return json(result)
