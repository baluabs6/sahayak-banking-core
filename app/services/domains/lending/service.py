"""
Lending domain service (scaffold).
Cash-flow-based underwriting for MSMEs/individuals lacking collateral:
combines the inclusion domain's credit score with loan-specific factors
(GST data availability, bank statement history, collateral) into an
approve/review/reject decision. Notifies via SNS on status change.
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postgres_models import LoanApplication, User
from app.services.aws.sns_service import SNSService
from app.services.aws.cloudwatch_service import CloudWatchService

_AUTO_APPROVE_SCORE_THRESHOLD = 700
_AUTO_REJECT_SCORE_THRESHOLD = 450


class LendingService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.sns = SNSService()
        self.cloudwatch = CloudWatchService()

    def _decide(self, latest_credit_score: int | None, application: dict) -> str:
        """Simple rule-based decision layer. In production this would call
        a trained underwriting model (e.g. XGBoost) taking the credit score
        as one of several features, with a human-in-the-loop for borderline
        cases — this scaffold keeps the rule explicit and auditable."""
        if latest_credit_score is None:
            return "under_review"
        if latest_credit_score >= _AUTO_APPROVE_SCORE_THRESHOLD and application.get("bank_statement_months_provided", 0) >= 6:
            return "approved"
        if latest_credit_score <= _AUTO_REJECT_SCORE_THRESHOLD:
            return "rejected"
        return "under_review"

    async def submit_application(self, user: User, application_data: dict, latest_credit_score: int | None) -> dict:
        decision = self._decide(latest_credit_score, application_data)

        application = LoanApplication(
            id=str(uuid.uuid4()),
            application_ref=f"LN-{uuid.uuid4().hex[:8].upper()}",
            user_id=user.id,
            loan_type=application_data["loan_type"],
            requested_amount_inr=application_data["requested_amount_inr"],
            purpose=application_data.get("purpose", ""),
            collateral_provided=application_data.get("collateral_provided", False),
            gst_data_available=application_data.get("gst_data_available", False),
            bank_statement_months_provided=application_data.get("bank_statement_months_provided", 0),
            status=decision,
        )
        self.db.add(application)
        await self.db.commit()

        try:
            self.sns.publish_loan_status_update(user.id, application.application_ref, decision)
            self.cloudwatch.record_loan_decision(decision)
        except Exception:
            pass  # non-fatal without live AWS creds

        return {
            "application_ref": application.application_ref,
            "status": decision,
            "used_credit_score": latest_credit_score,
        }
