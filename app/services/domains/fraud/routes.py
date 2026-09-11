"""
Fraud domain routes — BlackSheep Router.
Mounted at /api/v1/fraud in app/main.py.
"""
import uuid

from blacksheep import Request, json
from blacksheep.server.controllers import APIController, get, post
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import AsyncSessionLocal
from app.models.postgres_models import Transaction, User
from app.services.domains.fraud.service import FraudService


class FraudController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/fraud"

    @post("/transactions/evaluate")
    async def evaluate_transaction(self, request: Request) -> json:
        """Submit a transaction for real-time fraud evaluation."""
        payload = await request.json()
        required = {"user_ref", "amount_inr", "txn_type", "merchant", "location", "device_id"}
        missing = required - payload.keys()
        if missing:
            return json({"error": f"Missing fields: {missing}"}, status=400)

        async with AsyncSessionLocal() as db:  # type: AsyncSession
            user_result = await db.execute(select(User).where(User.user_ref == payload["user_ref"]))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            txn = Transaction(
                id=str(uuid.uuid4()),
                txn_ref=payload.get("txn_ref", f"TXN-{uuid.uuid4().hex[:8]}"),
                user_id=user.id,
                amount_inr=payload["amount_inr"],
                txn_type=payload["txn_type"],
                merchant=payload["merchant"],
                location=payload["location"],
                device_id=payload["device_id"],
            )
            db.add(txn)
            await db.flush()

            service = FraudService(db)
            result = await service.evaluate_transaction(txn)
            return json(result)

    @get("/events/{user_ref}")
    async def get_fraud_events(self, user_ref: str) -> json:
        """Fetch recent fraud events for a user from MongoDB."""
        from app.db.mongo import COLLECTION_FRAUD_EVENTS, get_mongo_db

        db_mongo = get_mongo_db()
        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

        cursor = db_mongo[COLLECTION_FRAUD_EVENTS].find({"user_id": user.id}).sort("created_at", -1).limit(20)
        events = [{**e, "_id": str(e["_id"])} async for e in cursor]
        return json({"user_ref": user_ref, "events": events})

    @post("/investigate/{user_ref}")
    async def investigate(self, request: Request, user_ref: str) -> json:
        """Investigator agent: given a flagged transaction (body: the dict
        returned by /transactions/evaluate), queries velocity/history/profile
        and RECOMMENDS an action for human confirmation — never executes one.
        See app/services/domains/fraud/agent.py."""
        from app.services.domains.fraud.agent import FraudInvestigatorAgent

        payload = await request.json()
        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            agent = FraudInvestigatorAgent(db)
            result = await agent.investigate(user, payload)
            return json(result)

    @post("/case-file/{user_ref}")
    async def case_file(self, request: Request, user_ref: str) -> json:
        """SNS-triggered agent loop (lightweight): pre-drafts a case-file
        summary from the same context tools, so a human reviewer starts from
        a synthesis instead of raw logs."""
        from app.services.domains.fraud.agent import FraudInvestigatorAgent

        payload = await request.json()
        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            agent = FraudInvestigatorAgent(db)
            draft = await agent.draft_case_file(user, payload)
            return json({"user_ref": user_ref, "case_file": draft})
