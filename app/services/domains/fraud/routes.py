"""
Fraud domain routes — BlackSheep Router.
Mounted at /api/v1/fraud in app/main.py.

Auth: requires a valid Bearer token. Submitting a transaction is restricted
to the account owner (or analyst/admin acting on behalf of ops tooling);
reading fraud events is analyst/admin-only by default, since that's a
risk-review workflow, not a customer-facing one.
"""
import uuid

from blacksheep import Request, json
from blacksheep.server.controllers import APIController, get, post
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit_event
from app.core.auth_middleware import ensure_owner_or_role, require_identity
from app.db.postgres import AsyncSessionLocal
from app.db.redis_client import check_rate_limit
from app.models.postgres_models import Transaction, User
from app.services.domains.fraud.schemas import TransactionEvaluateRequest
from app.services.domains.fraud.service import FraudService

_ANALYST_ROLES = ("analyst", "admin")


class FraudController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/fraud"

    @post("/transactions/evaluate")
    async def evaluate_transaction(self, request: Request) -> json:
        """Submit a transaction for real-time fraud evaluation."""
        identity = require_identity(request)

        raw = await request.json()
        try:
            body = TransactionEvaluateRequest.model_validate(raw)
        except ValidationError as e:
            return json({"error": "Invalid request", "details": e.errors()}, status=400)

        deny = ensure_owner_or_role(identity, body.user_ref, allowed_roles=_ANALYST_ROLES)
        if deny:
            return deny

        allowed, _ = await check_rate_limit(identity["sub"], route="fraud_evaluate", limit=20, window_seconds=60)
        if not allowed:
            return json({"error": "Rate limit exceeded. Try again shortly."}, status=429)

        async with AsyncSessionLocal() as db:  # type: AsyncSession
            user_result = await db.execute(select(User).where(User.user_ref == body.user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            txn = Transaction(
                id=str(uuid.uuid4()),
                txn_ref=body.txn_ref or f"TXN-{uuid.uuid4().hex[:8]}",
                user_id=user.id,
                amount_inr=body.amount_inr,
                txn_type=body.txn_type,
                merchant=body.merchant,
                location=body.location,
                device_id=body.device_id,
            )
            db.add(txn)
            await db.flush()

            service = FraudService(db)
            result = await service.evaluate_transaction(txn)

        await log_audit_event(
            action="fraud.transaction_evaluate", actor_ref=identity["sub"], actor_role=identity.get("role"),
            target_user_ref=body.user_ref,
            details={"txn_ref": result.get("txn_ref"), "action": result.get("action"), "is_flagged": result.get("is_flagged")},
        )
        return json(result)

    @get("/events/{user_ref}")
    async def get_fraud_events(self, request: Request, user_ref: str) -> json:
        """Fetch recent fraud events for a user from MongoDB. Analyst/admin
        only — this is a risk-review surface, not exposed to end customers."""
        from app.db.mongo import COLLECTION_FRAUD_EVENTS, get_mongo_db

        identity = require_identity(request)
        deny = ensure_owner_or_role(identity, user_ref, allowed_roles=_ANALYST_ROLES)
        if deny:
            return deny

        db_mongo = get_mongo_db()
        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

        cursor = db_mongo[COLLECTION_FRAUD_EVENTS].find({"user_id": user.id}).sort("created_at", -1).limit(20)
        events = [{**e, "_id": str(e["_id"])} async for e in cursor]

        await log_audit_event(
            action="fraud.events_view", actor_ref=identity["sub"], actor_role=identity.get("role"),
            target_user_ref=user_ref, details={"count": len(events)},
        )
        return json({"user_ref": user_ref, "events": events})
