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

    @post("/claims/{claim_ref}/triage")
    async def triage_claim(self, claim_ref: str) -> json:
        """Claims triage agent for non-parametric (manual-assessment)
        claims: drafts a recommended settlement grounded in policy/scheme
        documents via the RAG layer. Advisory only — a human assessor
        still confirms the settlement."""
        from app.services.domains.insurance.agent import InsuranceClaimsAgent

        async with AsyncSessionLocal() as db:
            from app.models.postgres_models import InsuranceClaim

            claim_result = await db.execute(select(InsuranceClaim).where(InsuranceClaim.claim_ref == claim_ref))
            claim = claim_result.scalar_one_or_none()
            if claim is None:
                return json({"error": "Unknown claim_ref"}, status=404)

            claim_dict = {
                "policy_type": claim.policy_type,
                "trigger_event": claim.trigger_event,
                "region": claim.region,
                "claim_amount_inr": float(claim.claim_amount_inr),
            }

            kyc_docs = None
            try:
                from app.db.mongo import COLLECTION_KYC_DOCUMENTS, get_mongo_db

                db_mongo = get_mongo_db()
                record = await db_mongo[COLLECTION_KYC_DOCUMENTS].find_one({"user_id": claim.user_id})
                kyc_docs = record.get("documents") if record else None
            except Exception:
                kyc_docs = None

            agent = InsuranceClaimsAgent()
            result = await agent.triage_claim(claim.user_id, claim_dict, kyc_documents=kyc_docs)
            return json({"claim_ref": claim_ref, **result})
