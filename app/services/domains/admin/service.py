"""
Admin/analyst domain service.

Three previously-missing operational capabilities, all analyst/admin-only:
  1. A dashboard summary — flagged transactions, pending loan reviews,
     claims in queue, and agents awaiting human confirmation, aggregated
     in one call instead of an analyst hitting four different domain
     routes and adding it up themselves.
  2. A queue of agent actions that used the propose-then-confirm pattern
     and are still awaiting a human decision, plus the ability to record
     that decision.
  3. A queryable view of the append-only audit log, since it was being
     written everywhere but never read back through the API.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mongo import COLLECTION_AGENT_AUDIT_LOG, COLLECTION_AUDIT_LOGS, get_mongo_db
from app.models.postgres_models import InsuranceClaim, LoanApplication, Transaction


class AdminService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def dashboard_summary(self) -> dict:
        flagged_txns = await self.db.scalar(select(func.count()).select_from(Transaction).where(Transaction.is_flagged.is_(True)))
        pending_loans = await self.db.scalar(select(func.count()).select_from(LoanApplication).where(LoanApplication.status == "under_review"))
        queued_claims = await self.db.scalar(select(func.count()).select_from(InsuranceClaim).where(InsuranceClaim.status == "filed"))

        mongo_db = get_mongo_db()
        pending_agent_actions = await mongo_db[COLLECTION_AGENT_AUDIT_LOG].count_documents({"confirmed": False})

        return {
            "flagged_transactions": flagged_txns or 0,
            "pending_loan_reviews": pending_loans or 0,
            "claims_in_queue": queued_claims or 0,
            "pending_agent_actions": pending_agent_actions,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def list_pending_agent_actions(self, limit: int = 50) -> list[dict]:
        mongo_db = get_mongo_db()
        cursor = mongo_db[COLLECTION_AGENT_AUDIT_LOG].find({"confirmed": False}).sort("created_at", -1).limit(limit)
        actions = []
        async for doc in cursor:
            actions.append(
                {
                    "id": str(doc["_id"]),
                    "agent_name": doc.get("agent_name"),
                    "user_id": doc.get("user_id"),
                    "proposed_action": doc.get("proposed_action"),
                    "final_text": doc.get("final_text"),
                    "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
                }
            )
        return actions

    async def confirm_agent_action(self, action_id: str, confirmed_by: str, approve: bool) -> dict:
        from bson import ObjectId
        from bson.errors import InvalidId

        mongo_db = get_mongo_db()
        try:
            object_id = ObjectId(action_id)
        except InvalidId:
            return {"error": "Invalid action id"}

        existing = await mongo_db[COLLECTION_AGENT_AUDIT_LOG].find_one({"_id": object_id})
        if existing is None:
            return {"error": "No pending agent action found for that id"}
        if existing.get("confirmed") is None:
            return {"error": "This agent run never proposed a consequential action requiring confirmation"}
        if existing.get("confirmed") is True:
            return {"error": "This action was already confirmed"}

        await mongo_db[COLLECTION_AGENT_AUDIT_LOG].update_one(
            {"_id": object_id},
            {
                "$set": {
                    "confirmed": bool(approve),
                    "confirmed_by": confirmed_by,
                    "confirmed_at": datetime.now(timezone.utc),
                }
            },
        )
        return {"id": action_id, "confirmed": bool(approve), "confirmed_by": confirmed_by}

    async def query_audit_log(self, target_user_ref: str | None, action: str | None, limit: int = 100) -> list[dict]:
        mongo_db = get_mongo_db()
        query: dict = {}
        if target_user_ref:
            query["target_user_ref"] = target_user_ref
        if action:
            query["action"] = action

        cursor = mongo_db[COLLECTION_AUDIT_LOGS].find(query).sort("created_at", -1).limit(limit)
        events = []
        async for doc in cursor:
            events.append(
                {
                    "action": doc.get("action"),
                    "actor_ref": doc.get("actor_ref"),
                    "actor_role": doc.get("actor_role"),
                    "target_user_ref": doc.get("target_user_ref"),
                    "outcome": doc.get("outcome"),
                    "details": doc.get("details"),
                    "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
                }
            )
        return events
