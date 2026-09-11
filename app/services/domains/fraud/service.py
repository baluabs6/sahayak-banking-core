"""
Fraud domain service — orchestrates: ML+rule detection -> Postgres update
-> Mongo event log -> DynamoDB velocity counter -> SNS alert -> CloudWatch
metric -> optional LLM-generated risk narrative.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mongo import COLLECTION_FRAUD_EVENTS, get_mongo_db
from app.models.mongo_models import FraudEvent
from app.models.postgres_models import Transaction, User
from app.services.aws.cloudwatch_service import CloudWatchService
from app.services.aws.dynamodb_service import DynamoDBService
from app.services.aws.sns_service import SNSService
from app.services.ai.llm_provider import get_llm_provider
from app.services.domains.fraud.anomaly_detection import FraudDetectionEngine

_engine = FraudDetectionEngine()  # process-wide singleton; retrained on a schedule in production


class FraudService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.dynamodb = DynamoDBService()
        self.sns = SNSService()
        self.cloudwatch = CloudWatchService()

    async def evaluate_transaction(self, txn: Transaction, generate_narrative: bool = True) -> dict:
        # Pull user's recent history for feature context
        history_result = await self.db.execute(
            select(Transaction).where(Transaction.user_id == txn.user_id).order_by(Transaction.timestamp.desc()).limit(50)
        )
        history = [
            {
                "amount_inr": float(h.amount_inr),
                "timestamp": h.timestamp,
                "location": h.location,
                "device_id": h.device_id,
                "txn_type": h.txn_type,
            }
            for h in history_result.scalars().all()
        ]

        txn_dict = {
            "amount_inr": float(txn.amount_inr),
            "timestamp": txn.timestamp,
            "location": txn.location,
            "device_id": txn.device_id,
            "txn_type": txn.txn_type,
        }

        if history:
            _engine.fit(history)

        # DynamoDB velocity counter (per-minute window). Incremented BEFORE
        # scoring, and keyed on wall-clock time (not txn.timestamp, which
        # previously fell back to the literal string "now" whenever the
        # ORM-default timestamp hadn't been populated yet — that made every
        # such transaction land in the same bucket forever instead of a
        # real per-minute window). The resulting count is fed into
        # check_transaction so a velocity spike can actually flag a
        # transaction — previously this counter was written but never read.
        velocity_count = None
        window_key = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        try:
            velocity_count = self.dynamodb.increment_txn_velocity(txn.user_id, window_key)
        except Exception:
            pass  # non-fatal in demo/local mode without live AWS creds

        result = _engine.check_transaction(txn_dict, history, velocity_count=velocity_count)

        # Update Postgres (system of record)
        txn.is_flagged = result.is_flagged
        txn.fraud_score = result.ml_anomaly_score
        await self.db.commit()

        narrative = None
        if generate_narrative and result.is_flagged:
            provider = get_llm_provider()
            narrative = await provider.complete(
                system_prompt=(
                    "You write short, plain-language fraud alert explanations for bank "
                    "risk analysts reviewing a queue. Be factual and concise, 2 sentences max."
                ),
                user_prompt=(
                    f"Transaction amount: INR {txn.amount_inr}, type: {txn.txn_type}, "
                    f"location: {txn.location}, rule triggers: {result.rule_triggers}, "
                    f"ML anomaly score: {result.ml_anomaly_score}."
                ),
            )

        # Mongo event log
        event = FraudEvent(
            user_id=txn.user_id,
            txn_ref=txn.txn_ref,
            rule_triggers=result.rule_triggers,
            ml_anomaly_score=result.ml_anomaly_score,
            llm_risk_narrative=narrative,
            action_taken=result.action,
            created_at=datetime.now(timezone.utc),
        )
        db_mongo = get_mongo_db()
        await db_mongo[COLLECTION_FRAUD_EVENTS].insert_one(event.model_dump())

        # Alert + metric
        if result.is_flagged:
            try:
                self.sns.publish_fraud_alert(txn.user_id, txn.txn_ref, ", ".join(result.rule_triggers) or "ml_anomaly", result.ml_anomaly_score)
            except Exception:
                pass
            try:
                self.cloudwatch.record_fraud_flag()
            except Exception:
                pass

        return {
            "txn_ref": txn.txn_ref,
            "is_flagged": result.is_flagged,
            "action": result.action,
            "ml_anomaly_score": result.ml_anomaly_score,
            "rule_triggers": result.rule_triggers,
            "risk_narrative": narrative,
        }
