"""
Insurance domain service (scaffold).
Parametric insurance: claims for crop/weather/flood policies can be
auto-triggered from external index data (rainfall deficit, flood index)
instead of waiting on manual assessment — the core fix for slow/disputed
crop insurance payouts.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postgres_models import InsuranceClaim, User

# Policy types eligible for automatic, index-based payout
_PARAMETRIC_POLICY_TYPES = {"crop_insurance", "shop_property"}


class InsuranceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _should_auto_trigger(self, policy_type: str, index_value: float | None, threshold: float | None) -> bool:
        if policy_type not in _PARAMETRIC_POLICY_TYPES:
            return False
        if index_value is None or threshold is None:
            return False
        return index_value >= threshold  # e.g. rainfall deficit % or flood index over threshold

    async def file_claim(self, user: User, claim_data: dict) -> dict:
        auto_trigger = self._should_auto_trigger(
            claim_data["policy_type"],
            claim_data.get("index_value"),
            claim_data.get("index_threshold"),
        )
        status = "paid" if auto_trigger else "under_review"

        # Anomalous-claim check before any auto-payout goes out — a claim
        # that would otherwise auto-trigger but looks anomalous against the
        # user's recent claim history gets routed to manual/fraud review
        # instead. Previously auto-triggered claims paid out unconditionally
        # with no check against the user's other recent claims at all.
        route_to_fraud_review = False
        if auto_trigger:
            history_result = await self.db.execute(
                select(InsuranceClaim).where(InsuranceClaim.user_id == user.id).order_by(InsuranceClaim.filed_at.desc()).limit(10)
            )
            recent_claims = [
                {
                    "policy_type": c.policy_type,
                    "claim_amount_inr": float(c.claim_amount_inr),
                    "auto_triggered": c.auto_triggered,
                    "status": c.status,
                }
                for c in history_result.scalars().all()
            ]
            from app.services.domains.insurance.agent import InsuranceClaimsAgent

            try:
                flag_result = await InsuranceClaimsAgent().flag_anomalous_claim(user.id, claim_data, recent_claims)
                route_to_fraud_review = flag_result["route_to_fraud_review"]
            except Exception:
                route_to_fraud_review = False  # best-effort; never block a legitimate auto-payout on a check failure

            if route_to_fraud_review:
                status = "under_review"
                auto_trigger = False

        claim = InsuranceClaim(
            id=str(uuid.uuid4()),
            claim_ref=f"CLM-{uuid.uuid4().hex[:8].upper()}",
            user_id=user.id,
            policy_type=claim_data["policy_type"],
            trigger_event=claim_data.get("trigger_event", ""),
            region=claim_data.get("region", ""),
            claim_amount_inr=claim_data["claim_amount_inr"],
            parametric_data_source=claim_data.get("parametric_data_source"),
            auto_triggered=auto_trigger,
            status=status,
        )
        self.db.add(claim)
        await self.db.commit()

        return {
            "claim_ref": claim.claim_ref,
            "status": status,
            "auto_triggered": auto_trigger,
            "routed_to_fraud_review": route_to_fraud_review,
        }
